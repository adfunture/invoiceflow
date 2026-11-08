# InvoiceFlow – Architektur

InvoiceFlow holt Eingangsrechnungen aus festgelegten IMAP-Postfächern von
**SBC symbio GmbH & Co. KG** (ALL-INKL) und **Adfunture Outdoor GmbH** (STRATO),
erkennt und prüft sie und überträgt sie genau einmal an den jeweils
richtigen DATEV-Unternehmen-online-Bestand.

## Grundprinzipien

1. **Mandantentrennung in der Datenbank, nicht nur im Code.** Jede
   geschäftliche Tabelle trägt `company_id`. Beziehungen laufen über
   zusammengesetzte Fremdschlüssel `(company_id, id)`. Ein Dokument kann
   dadurch technisch nicht an die E-Mail oder das Postfach eines anderen
   Unternehmens gehängt werden.
2. **Die Konfiguration gewinnt, der Inhalt nie.** Unternehmenszuordnung
   (Postfach → Unternehmen) und DATEV-Zieladresse kommen ausschließlich aus
   der Datenbank-Konfiguration. Text in E-Mails, PDFs, XML oder in einer
   KI-Antwort kann sie nicht ändern. KI-Ausgaben werden nur als
   schema-validierte Datenfelder übernommen.
3. **Höchstens einmal an DATEV.** Doppelte Übertragung wird durch
   Unique-Constraints in `datev_transmissions` verhindert (Datei-Hash und
   Rechnungsidentität je Unternehmen), nicht durch Programmlogik allein.
   Ein unklarer SMTP-Ausgang (`sending`/`sent_unconfirmed`) blockiert jeden
   automatischen Zweitversand.
4. **Idempotenter Import.** IMAP-Nachrichten sind über
   `(mailbox, ordner, UIDVALIDITY, UID)` eindeutig. Erneuter Abruf, Neustart
   oder paralleler Worker erzeugt keine Doppelimporte.
5. **Originale bleiben unverändert.** Dateien werden mit SHA-256 gehasht,
   AES-256-GCM-verschlüsselt im Objektspeicher abgelegt und nie
   überschrieben (Content-addressed Keys).
6. **Automatischer DATEV-Versand ist standardmäßig aus.** Er lässt sich erst
   einschalten, wenn alle Freigabeschritte je Unternehmen dokumentiert sind
   (siehe „Produktivfreigabe“). Die Regel ist zusätzlich als
   CHECK-Constraint in der Datenbank hinterlegt.

## Komponenten

```
                ┌────────────────────────── Railway-Projekt „invoiceflow“ (EU) ───────────────────────────┐
                │                                                                                          │
  Browser ──TLS─┤─► web            FastAPI (API + ausgeliefertes React-Dashboard), Auth, Healthchecks      │
                │      │                                                                                   │
                │      ▼                                                                                   │
                │   PostgreSQL ◄──── worker-imap        IMAP-Abruf alle 5 Min., Backfill ab 08.11.2026       │
                │      ▲  ▲                                                                                │
                │      │  └───────── worker-processing  MIME, Klassifikation, Extraktion, Dubletten, KI     │
                │      │                                                                                   │
                │      └──────────── worker-datev       DATEV Upload Mail (SMTP), XML-Warteschlange         │
                │                                                                                          │
                │   Bucket (S3-kompatibel)  ◄── alle Services: verschlüsselte Originale (.eml, PDF, XML)      │
                └──────────────────────────────────────────────────────────────────────────────────────────┘
        IMAP/SMTP: imap.strato.de / smtp.strato.de (Adfunture), ALL-INKL-Server (SBC)
        DATEV:     <uuid>@uploadmail.datev.de je Unternehmen (nur aus Konfiguration)
```

| Railway-Service    | Start-Befehl                          | Aufgabe |
|--------------------|---------------------------------------|---------|
| `web`              | `uvicorn invoiceflow.api.app:app`     | REST-API, Dashboard, Login, Healthcheck `/health/ready`. Führt vor jedem Deploy `invoiceflow migrate` aus. |
| `worker-imap`      | `invoiceflow worker imap` (Phase 2)   | Postfächer abrufen, Rohnachricht + Anhänge speichern, Jobs anlegen. |
| `worker-processing`| `invoiceflow worker processing` (Phase 3) | Klassifikation, Extraktion, Dublettenprüfung, Statusentscheid. |
| `worker-datev`     | `invoiceflow worker datev` (Phase 4)  | Versand per DATEV Upload Mail, Versandprotokoll, XML-Warteschlange. |
| `ops-monitor` (Cron) | `invoiceflow ops monitor` + `maintenance` | Alle 10 min: Überwachung, Alarme, technische Bereinigung. |
| `ops-backup` (Cron) | `invoiceflow ops backup` + `verify-storage` | Täglich: verschlüsseltes DB-Backup, Speicherstichprobe. |
| `ops-restore-check` (Cron) | `invoiceflow ops restore-check` | Wöchentlich: Backup ist wiederherstellbar. |
| `Postgres`         | Railway-Plugin                        | Alle Metadaten, Jobs, Audit-Log. |
| `Bucket`           | Railway Bucket (S3-API)               | Verschlüsselte Originaldokumente. Kein Railway-Volume: Volumes hängen an genau einem Service und überleben Re-Deploys nicht über Services hinweg. |

Alle Services nutzen dasselbe Docker-Image; nur der Start-Befehl
unterscheidet sich. Die Worker sind getrennt, damit ein hängender
IMAP-Server den DATEV-Versand nicht blockiert und der Versand-Worker separat
gestoppt werden kann.

## Job-Verarbeitung (ab Phase 2)

`processing_jobs` ist die Warteschlange. Worker holen Jobs mit
`SELECT … FOR UPDATE SKIP LOCKED`, setzen einen Lease (`lease_expires_at`)
und verlängern ihn während der Arbeit. Stirbt ein Container mitten im Job,
läuft der Lease ab und ein anderer Worker übernimmt. `dedup_key` ist unique,
damit derselbe Arbeitsschritt nicht doppelt eingeplant wird.

Ausnahme DATEV-Versand: Ein Job, dessen Transmission bereits `sending`
war, wird **nicht** neu versendet, sondern auf `manual_review` gesetzt –
man weiß nicht, ob die SMTP-Übergabe vor dem Absturz geklappt hat.

## Datenmodell

| Tabelle | Zweck | Wichtige Constraints |
|---------|-------|----------------------|
| `companies` | Die zwei Unternehmen | `slug` unique |
| `mailboxes` | Postfach → Unternehmen (fest) | E-Mail unique, `credential_ref` nur Name einer Secret-Variable, aktiv nur mit bestätigter Domain |
| `mailbox_sync_states` | Pro Ordner UIDVALIDITY + letzte UID | `(mailbox, ordner)` unique |
| `email_messages` | Importierte Nachricht (Metadaten, Speicher-Key der .eml) | `(mailbox, ordner, uidvalidity, uid)` unique |
| `email_attachments` | Jeder MIME-Teil einzeln | `(message, part_index)` unique |
| `documents` | Ein zu verarbeitendes Dokument (PDF/XML/ZUGFeRD) | eins pro Anhang |
| `invoice_metadata` | Extrahierte Rechnungsdaten, `identity_hash` | eins pro Dokument |
| `invoice_duplicates` | Dublettenbeziehungen inkl. Prüfentscheid | Paar unique |
| `datev_destinations` | DATEV-Zieladresse + SMTP-Absender + Freigabeschritte | eine je Unternehmen und Kanal; CHECK für Freigabe |
| `datev_transmissions` | Ein Versand je Dokument | unique je `(company, file_sha256)` und `(company, identity_hash)` |
| `datev_transmission_attempts` | Jeder Versuch mit SMTP-Ergebnis | – |
| `processing_jobs` | Warteschlange | `dedup_key` unique |
| `audit_logs` | Wer hat wann was geändert | append-only |
| `application_settings` | Testmodus, Aufbewahrung usw. | `(company, key)` unique |
| `users`, `user_company_access`, `user_sessions`, `login_attempts` | Anmeldung, Rollen, Brute-Force-Schutz | – |

## Sicherheit

* **Passwörter für Postfächer** liegen nie in PostgreSQL. `mailboxes.credential_ref`
  enthält nur den Namen einer Railway-Variable (Präfix `INVOICEFLOW_SECRET_`).
  Der Resolver liest ausschließlich Variablen mit diesem Präfix, so dass sich
  über die Konfiguration z. B. nicht `DATABASE_URL` auslesen lässt.
* **Anmeldung:** Argon2id-Hashes, Session-Token als HttpOnly/Secure/SameSite=Strict-Cookie,
  in der DB nur als SHA-256 gespeichert; CSRF-Schutz per Double-Submit-Header;
  absolute und Leerlauf-Ablaufzeit; Sperre nach wiederholten Fehlversuchen
  pro Konto und pro IP.
* **Rollen:** `admin` (Konfiguration, Freigaben), `reviewer` (Prüfen,
  manuelle Freigabe einzelner Dokumente), `viewer` (lesen). Nicht-Admins
  sehen nur die ihnen zugeordneten Unternehmen.
* **Transport:** Railway terminiert TLS; die App setzt HSTS und lehnt in
  Produktion unverschlüsselte Weiterleitungen ab. IMAP/SMTP nur mit TLS.
* **Dateien:** Größenlimits, Typprüfung per Inhalt, XML nur mit `defusedxml`
  (kein XXE), Links in E-Mails werden nicht automatisch abgerufen (SSRF).
* **Logs:** JSON-strukturiert; Felder wie `password`, `token`, `secret`
  werden maskiert.

## Produktivfreigabe DATEV (je Unternehmen)

Der automatische Versand lässt sich erst einschalten, wenn in
`datev_destinations` alle Schritte mit Zeitpunkt und Benutzer dokumentiert sind:

1. Zieladresse verifiziert (Admin tippt sie zur Bestätigung erneut ein)
2. SMTP-Absender bei DATEV autorisiert
3. Unternehmenszuordnung getestet
4. Dublettenprüfung getestet
5. Testrechnungen in DATEV angekommen
6. Ausdrückliche Produktivfreigabe durch einen Admin

Ändert jemand die Zieladresse oder den Absender, werden alle Schritte
zurückgesetzt und der automatische Versand abgeschaltet. Zusätzlich gilt der
globale Testmodus (`datev.test_mode`, Standard: an) und die
Umgebungsvariable `DATEV_ALLOW_REAL_SEND` (Standard: `false`). Erst wenn alle
drei Ebenen es erlauben, verlässt eine Mail das System Richtung DATEV.
