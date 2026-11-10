# Deployment auf Railway

Region: **EU West (Amsterdam)** – Rechnungs- und E-Mail-Daten bleiben in der EU.

## 0. Empfohlen: Infrastructure as Code

Alle Services, Datenbank, Buckets, Cron-Zeitpläne und Variablen-Verknüpfungen stehen in
[`.railway/railway.ts`](../.railway/railway.ts). Die Abschnitte 1–9 beschreiben dieselbe Einrichtung
von Hand und bleiben als Referenz.

```bash
npm install                      # im Repo-Root, nur das Railway-IaC-Paket
railway init                     # Projekt "invoiceflow" im Workspace adfunture
railway link                     # im Repo-Root
# Project Settings → Standardregion EU West, BEVOR apply die Datenbank anlegt
npx railway config plan          # zeigt die Änderungen, ändert nichts
npx railway config apply         # fragt vor dem Anwenden nach
```

Danach im Dashboard: Postgres-Backups aktivieren, Domain für `web` erzeugen, alle `preserve()`-Variablen
(Secrets) setzen, ersten Admin per `railway ssh` anlegen (Abschnitt 3).

## 1. Projekt und Datenbank

1. Neues Railway-Projekt `invoiceflow` anlegen, Region EU.
2. **PostgreSQL** hinzufügen (Railway-Template). Backups im Postgres-Service
   aktivieren (siehe Abschnitt Backups).
3. GitHub-Repo verbinden.

## 2. Service `web`

| Einstellung | Wert |
|---|---|
| Source | dieses Repo, Branch `main` |
| Config-as-code | `railway/web.json` |
| Public Networking | Domain generieren (oder eigene Subdomain) |

Variablen:

| Variable | Wert |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `APP_ENV` | `production` |
| `COOKIE_SECURE` | `true` |
| `ALLOWED_HOSTS` | `["<deine-domain>"]` |
| `DATEV_ALLOW_REAL_SEND` | `false` (bis zur Produktivfreigabe) |
| `DOCUMENT_ENCRYPTION_KEY` | `${{shared.DOCUMENT_ENCRYPTION_KEY}}` |
| `STORAGE_BACKEND` | `s3` |
| `S3_*` | Zugangsdaten des Buckets (Abschnitt 5) |
| `INVOICEFLOW_SECRET_IMAP_*` | Postfach-Passwörter (siehe `.env.example`) |
| `ANTHROPIC_API_KEY` | `${{shared.ANTHROPIC_API_KEY}}` – nur `web` und `worker-processing` (setzt die IaC) |

Wichtig: Die Shared Variable `ANTHROPIC_API_KEY` **vor** `railway config apply` im Projekt anlegen
(Project Settings → Shared Variables), sonst zeigt die Referenz ins Leere. Ob der Schlüssel im
Processing-Worker ankommt, zeigt das Dashboard (Hintergrunddienste → „KI-Schlüssel“); `ops-monitor`
warnt, wenn der KI-Fallback eingeschaltet ist, der Schlüssel aber fehlt.

Tipp: Gemeinsame Werte (`DOCUMENT_ENCRYPTION_KEY`, `S3_*`,
`INVOICEFLOW_SECRET_*`) als **Shared Variables** im Projekt anlegen und in
`web` und den Workern referenzieren.

Vor jedem Deploy laufen automatisch `invoiceflow migrate` und `invoiceflow seed`
(`preDeployCommand`). Der Healthcheck `/health/ready` schlägt fehl, wenn die
Datenbank nicht erreichbar ist oder das Schema nicht auf dem neuesten Stand ist.

## 3. Ersten Admin anlegen

```bash
railway link            # Projekt invoiceflow, Service web
railway ssh             # Shell im laufenden web-Container
invoiceflow create-user --email sascha.senninger@sbc-symbio.de --role admin --must-change-password
```

## 4. Offene Konfiguration prüfen

```bash
invoiceflow check-config    # in der railway-ssh-Shell
```

Zeigt je Postfach, was vor der Aktivierung fehlt (Domain, IMAP-Host,
Railway-Variable), und je DATEV-Ziel die offenen Freigabeschritte sowie
Formatwarnungen der Adresse.

## 5. Dokumentenspeicher (Bucket)

1. Im Projekt einen S3-kompatiblen **Bucket** mit EU-Standort anlegen (Railway
   Bucket oder anderer S3-kompatibler Anbieter).
2. Zugangsdaten als `S3_BUCKET`, `S3_ENDPOINT_URL`, `S3_REGION`,
   `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` hinterlegen.
3. Schlüssel erzeugen: `invoiceflow generate-key` → `DOCUMENT_ENCRYPTION_KEY`.
   **Zusätzlich offline sichern** (Passwortmanager). Geht der Schlüssel
   verloren, sind alle gespeicherten Originale unlesbar.

Die Anwendung verschlüsselt selbst (AES-256-GCM); der Speicheranbieter sieht
nur Chiffrat. In Produktion startet die App ohne `STORAGE_BACKEND=s3` nicht.

## 6. Service `worker-imap`

| Einstellung | Wert |
|---|---|
| Source | gleiches Repo |
| Config-as-code | `railway/worker-imap.json` |
| Public Networking | keins |
| Variablen | wie `web` (DB, Speicher, Schlüssel, `INVOICEFLOW_SECRET_IMAP_*`) |

Der Worker ruft aktive Postfächer alle 5 Minuten ab – **alle Ordner inklusive Unterordner**. Neue Ordner
werden bei jedem Abruf erkannt und ab dem 08.11.2026 nachgeholt. Automatisch ausgeschlossen (im Dashboard
sichtbar und einzeln einschaltbar) sind Gesendet, Entwürfe, Papierkorb und Spam samt deren Unterordnern –
Gesendet enthält eigene Ausgangsrechnungen, Spam ist der häufigste Weg für gefälschte Rechnungen.
Der Worker schreibt einen Heartbeat
(`GET /api/workers`) und beendet sich bei einem Deploy nach der laufenden
Nachricht. Ein abgebrochener Abruf wird an der letzten vollständig
gespeicherten Nachricht fortgesetzt.

**Inbetriebnahme je Postfach:**

1. Railway-Variable `INVOICEFLOW_SECRET_IMAP_…` setzen.
2. IMAP-Host/Benutzer prüfen, dann `POST /api/mailboxes/{id}/test-connection`.
3. Bei `mail@` und `sascha.senninger@sbc-symbio.de`: Domain bestätigen.
4. Aktivieren (`POST /api/mailboxes/{id}/active`). Der erste Lauf holt alle
   Nachrichten seit 08.11.2026 00:00 (Europe/Berlin) nach.

Einmaliger Abruf zur Diagnose: `invoiceflow sync-mailbox info@adfunture.de`

## 7. Service `worker-processing`

| Einstellung | Wert |
|---|---|
| Config-as-code | `railway/worker-processing.json` |
| Public Networking | keins |
| Variablen | wie `web` (DB, Speicher, Schlüssel); optional `ANTHROPIC_API_KEY`, `AI_MODEL`, `AI_EFFORT` |

Verarbeitet jede importierte Nachricht: Dokumente erkennen, Dubletten prüfen,
Status setzen. Mehrere Replikas sind möglich.

**Optionale Einstellungen (Dashboard/API `PUT /api/settings/{key}`):**

| Schlüssel | Bedeutung |
|---|---|
| `ocr.provider` = `tesseract` | Scan-PDFs lokal per OCR lesen (Tesseract ist im Image) |
| `ai.enabled` = `true`, `ai.provider` = `anthropic` | KI-Fallback für unklare Belege – **erst nach AV-Vertrag** mit Anthropic. Daten gehen an die Anthropic-API. |
| `ai.min_confidence_percent` | darunter → manuelle Prüfung (Standard 90) |

## 8. Service `worker-datev`

| Einstellung | Wert |
|---|---|
| Config-as-code | `railway/worker-datev.json` (genau **eine** Replika) |
| Public Networking | keins |
| Variablen | wie `web` + `INVOICEFLOW_SECRET_SMTP_*` (SMTP-Passwort des Absenders); `DATEV_ALLOW_REAL_SEND` bleibt `false` bis Schritt 4 unten |

Der Worker legt für jeden Beleg im Status „bereit für DATEV“ eine Übertragung
an und sendet nur, wenn **alle** Sperren offen sind. Solange nicht, steht der
Grund in `last_error` der Übertragung (`GET /api/datev/transmissions`).

### Inbetriebnahme DATEV je Unternehmen (Reihenfolge einhalten)

1. **Zieladresse** aus DATEV Unternehmen online kopieren (nicht aus Screenshot):
   `PUT /api/datev/destinations/{id}/address`, dann `POST …/verify-address` mit erneuter Eingabe.
2. **SMTP-Absender** setzen (`PUT …/sender`, z. B. `invoice@adfunture.de` über `smtp.strato.de:465`),
   Passwort als Railway-Variable. Absender in DATEV als berechtigt hinterlegen → `POST …/gate/sender_authorized`.
3. **Zuordnung und Dubletten testen** (Testmails an beide Postfächer, Ergebnis in `/api/documents` prüfen) →
   `POST …/gate/assignment_tested`, `POST …/gate/duplicate_check_tested`.
4. **Testlauf** ohne Versand: `POST /api/datev/documents/{id}/dry-run` – zeigt Empfänger, Betreff, Anhang.
5. **Testlieferung** (echter Versand weniger Testbelege): `DATEV_ALLOW_REAL_SEND=true` setzen,
   Einstellung `datev.test_mode` = `false`, dann je Beleg
   `POST /api/datev/transmissions/{id}/send` mit `{"mode": "test_delivery"}` (nur Admin).
6. In DATEV prüfen, ob die Belege angekommen sind → `POST …/gate/test_delivery_confirmed`,
   Übertragungen bestätigen (`POST /api/datev/transmissions/{id}/confirm`).
7. **Produktivfreigabe** (`POST …/release` mit „PRODUKTIV FREIGEBEN“), dann `POST …/auto-send` `{"enabled": true}`.

Zum Anhalten genügt eine der Sperren: Testmodus an, Auto-Versand aus oder
`DATEV_ALLOW_REAL_SEND=false`. Bereits eingeplante Belege gehen dann zurück in „pending“.

### Unklarer Versand

Bricht die Verbindung während der SMTP-Übertragung ab oder stirbt der Worker
mitten im Versand, steht die Übertragung auf `manual_review`. Es wird **nicht**
automatisch erneut gesendet. In DATEV nachsehen und klären:
`POST /api/datev/transmissions/{id}/resolve` mit `was_sent` true/false (nur Admin).

### Noch zu klären

* **ZUGFeRD/Factur-X per Upload Mail:** standardmäßig zurückgehalten, bis geklärt ist, wie
  der DATEV-Bestand eingebettete XML verarbeitet. Danach Einstellung
  `datev.upload_mail_zugferd_enabled` = `true`.
* **Größenlimit** je Mail (`datev.max_mail_size_mb`, Standard 20 MB) mit DATEV abgleichen.
* **XRechnung (reines XML):** bleibt in `/api/datev/xml-queue`, bis eine dokumentierte
  DATEV-Schnittstelle angebunden ist; bis dahin manueller Upload über den Original-Download.

## 9. Betriebs-Crons

Gleiches Image, als Railway-Cron (Prozess endet nach dem Lauf). Variablen wie `web`
plus Alarm- und Backup-Variablen (siehe `.env.example`).

| Service | Config | Zeitplan (UTC) | Aufgabe |
|---|---|---|---|
| `ops-monitor` | `railway/ops-monitor.json` | alle 10 min | Überwachung + Alarme, technische Bereinigung |
| `ops-backup` | `railway/ops-backup.json` | täglich 01:30 | verschlüsseltes DB-Backup, Stichprobe Dokumentenspeicher |
| `ops-restore-check` | `railway/ops-restore-check.json` | So 04:00 | neuestes Backup entschlüsseln und mit `pg_restore --list` prüfen |

Manuell (in `railway ssh`):

```bash
invoiceflow ops go-live-check          # Bereitschaft für den Produktivbetrieb
invoiceflow ops monitor                # Befunde sofort prüfen
invoiceflow ops retention-report       # Aufbewahrungsfristen
invoiceflow ops verify-storage         # alle Originale entschlüsseln und Hash prüfen
invoiceflow user deactivate name@firma.de
```

Für eine externe Erreichbarkeitsüberwachung (z. B. Uptime-Dienst) `GET /health/ready` alle 5 Minuten abfragen.

## Backups

Siehe [BACKUP_RESTORE.md](BACKUP_RESTORE.md). Kurz: Railway-Backups aktivieren, zusätzlich
tägliches eigenes, verschlüsseltes Backup in einen getrennten Bucket, wöchentliche Prüfung,
`DOCUMENT_ENCRYPTION_KEY` offline sichern.

## Migrationen

Nur über `invoiceflow migrate` (= `alembic upgrade head`) gegen die jeweilige
Datenbank. Niemals Produktions-DB als Shadow-/Test-DB verwenden; die Tests
leeren ihre Datenbank vollständig.


## Alarme einrichten

Alarme laufen über `ops-monitor` (alle 10 Minuten). Ohne Kanal erscheinen sie nur im Dashboard.
Die Variablen werden als Shared Variables angelegt (alle Services nutzen sie, die IaC übernimmt sie mit `preserve()`).

**Telegram (empfohlen, aufs Handy):**
1. In Telegram `@BotFather` öffnen → `/newbot` → Name vergeben → Token kopieren.
2. Dem neuen Bot eine Nachricht schreiben (sonst darf er dir nicht schreiben).
3. Chat-ID ermitteln: `https://api.telegram.org/bot<TOKEN>/getUpdates` im Browser öffnen → `"chat":{"id":…}`.
4. Railway-Variablen: `INVOICEFLOW_SECRET_TELEGRAM_BOT=<Token>`, `ALERT_TELEGRAM_TOKEN_REF=INVOICEFLOW_SECRET_TELEGRAM_BOT`,
   `ALERT_TELEGRAM_CHAT_ID=<Chat-ID>`.

**E-Mail:** `ALERT_EMAIL_TO`, `ALERT_EMAIL_FROM`, `ALERT_SMTP_HOST`, `ALERT_SMTP_PORT` (465), `ALERT_SMTP_USERNAME`,
`ALERT_SMTP_CREDENTIAL_REF=INVOICEFLOW_SECRET_SMTP_ALERTS` und `INVOICEFLOW_SECRET_SMTP_ALERTS=<Passwort>`.

Danach im Dashboard: **Einstellungen → Alarme → Testalarm senden**. Pro Kanal wird angezeigt, ob es geklappt hat.

## Belege ohne E-Mail

- **Hochladen:** Rechnungen → „Belege hochladen“ (PDF, XML, Foto). Auf dem Handy öffnet das Feld Kamera und Dateien –
  so kommen auch Belege aus WhatsApp ins System (in WhatsApp teilen/speichern, dann hochladen).
- **Portal-Links:** Einstellung „Rechnungslinks automatisch laden“ – nur für dort eingetragene Domains, z. B.
  `invoice.stripe.com, pay.stripe.com`.
