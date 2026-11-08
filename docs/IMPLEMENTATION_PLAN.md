# InvoiceFlow – Umsetzungsplan

Stand: 08.11.2026. Jede Phase endet mit grünen Tests, bevor die nächste beginnt.

## Phase 1 – Grundsystem ✅

- Projektstruktur, `pyproject.toml`, Docker, docker-compose, Railway-Konfiguration
- Settings (pydantic-settings), strukturierte JSON-Logs mit Secret-Maskierung
- Vollständiges Datenbankschema aller Phasen als Alembic-Migration `0001`
  (damit die Mandanten- und Dubletten-Constraints von Anfang an gelten)
- Seed der zwei Unternehmen, fünf Postfächer (inaktiv), DATEV-Ziele (unverifiziert)
- Auth: Login/Logout, Sessions, CSRF, Brute-Force-Sperre, Rollen, Unternehmenszugriff
- API: Unternehmen, Postfächer (inkl. Domain-Bestätigung, Aktivierung, Ordner),
  DATEV-Ziele mit Freigabe-Checkliste, Einstellungen, Audit-Log
- Healthchecks `/health` und `/health/ready`
- CLI: `migrate`, `seed`, `create-user`, `check-config`

## Phase 2 – E-Mail-Import ✅

- IMAP nur lesend (EXAMINE + BODY.PEEK), TLS mit Zertifikatsprüfung, Retry/Backoff
- Backfill per `SEARCH SINCE` (1 Tag Puffer) + exakter Filter auf
  INTERNALDATE ≥ 08.11.2026 00:00 Europe/Berlin; danach inkrementell per UID
- UIDVALIDITY-Wechsel → Neuscan; Duplikate über Rohdaten-Hash ausgeschlossen
- Fortschritt (`last_seen_uid`) in derselben Transaktion wie die Nachricht → exakter Wiederanlauf
- Postfach-Lease gegen parallelen Abruf, Worker-Heartbeats, sauberes SIGTERM-Verhalten
- Speicher: AES-256-GCM, inhaltsadressiert, nie überschreiben, lokal oder S3
- MIME: RFC 2047/2231, Weiterleitungen (auch base64-kodiert), Inhaltserkennung,
  Größenlimits, Blockliste ausführbarer Dateien, Inline-Logos übersprungen
- Rechnungslinks werden erkannt, nie automatisch abgerufen; SSRF-Prüfung für späteren manuellen Abruf
- Job-Queue (`SKIP LOCKED`, Lease, Backoff, Dead-Letter); jede Nachricht → Job `process_message`
- API: Testverbindung, „Jetzt abrufen“, E-Mail-Liste/Detail, Worker-Status
- Tests: 4, 10, 11, 15 (Anhang), 17 (Job-Ebene, nur CI), 18 (Parser), 19, 20

## Phase 3 – Rechnungserkennung ✅

- PDF-Text (pypdf), eingebettete ZUGFeRD/Factur-X-XML, XRechnung UBL/CII über defusedxml (XXE/Billion Laughs abgewiesen)
- Regelbasierte Klassifikation (Überschriften statt Stichwortzählung) und Extraktion:
  Nummer, Datum, Leistungs-/Fälligkeitsdatum, Netto/USt/Brutto mit Summenprüfung,
  Währung, USt-ID (ohne Kunden-USt-ID), IBAN mit Prüfziffer, Lieferant, Referenzrechnung
- Scan-Erkennung; OCR-Schnittstelle mit lokaler Tesseract-Implementierung (im Docker-Image)
- KI-Fallback nur bei Unklarheit und nur wenn aktiviert: Claude über das Anthropic-SDK,
  strukturierte JSON-Ausgabe + Pydantic-Validierung, **jeder KI-Wert muss im Dokument stehen**,
  Schema ohne Unternehmen/Ziel/Status; Anbieter austauschbar (`AiProvider`), OpenAI-Anbindung offen
- Empfängerprüfung gegen das Postfach-Unternehmen → manuelle Prüfung, Zuordnung bleibt fest
- Dubletten Ebene 1 (Datei-Hash) und 2 (Rechnungsidentität inkl. Dokumentart) unter Advisory-Locks;
  gleiche Nummer mit abweichenden Daten → Prüfung statt Verwerfen (Korrekturen)
- Status: `ready_for_datev`, `awaiting_datev_xml_integration`, `manual_review`, `duplicate`, `ignored`
- API: Dokumentliste mit Filtern, Detail mit Dubletten, Original-Download (protokolliert),
  manuelle Prüfung (freigeben mit Korrekturen / ignorieren / als Dublette markieren)
- Worker `worker-processing`
- Tests: 1–3, 5–9, 11, 14–18 (17 echt parallel nur in CI); Regressionstest nach Layout eines echten Belegs

## Phase 4 – DATEV ✅

- Worker `worker-datev`: Übertragungen planen → freischalten → senden → abgebrochene Versände sichern
- Statusmaschine pending → ready → sending → sent_unconfirmed → confirmed, plus failed / manual_review
- Versandarten: automatisch (nur nach Produktivfreigabe + Auto-Versand), manuell (nach Freigabe),
  Testlieferung (Admin, nach Checklistenschritten 1–4), Testlauf (baut Mail, sendet nichts)
- Sperren: Testmodus, Checkliste/Freigabe, `DATEV_ALLOW_REAL_SEND`; vor jedem Versand erneut geprüft
- SMTP mit Klassifikation: Fehler vor DATA → Wiederholung (4xx/Netz, Backoff) oder endgültig (5xx);
  Abbruch während DATA oder Worker-Absturz → `manual_review`, **kein** automatischer Zweitversand
- Ein Beleg je Mail, Original unverändert, Zieladresse ausschließlich aus der Konfiguration,
  Versandprotokoll je Versuch (Zeitpunkt, Adresse, SMTP-Code/-Antwort, Fehler, Worker)
- Manuelle Bestätigung; `record_feedback` als Andockpunkt für spätere DATEV-Rückmeldungen
- ZUGFeRD per Upload Mail bis zur Klärung zurückgehalten; XML-Warteschlange mit
  `DatevXmlChannel`-Abstraktion (bewusst ohne erfundene Endpunkte)
- Tests: 3, 11, 12, 13, 17 (Versandebene, echt parallel nur in CI), alle Sperren, Testlauf, Testlieferung

## Phase 5 – Dashboard ✅

- React 18 + TypeScript + Tailwind 4 (Vite), vom Web-Service unter `/` ausgeliefert (Multi-Stage-Dockerfile)
- Startseite mit allen geforderten Kennzahlen (`GET /api/dashboard/summary`) und Worker-Status
- Rechnungsübersicht mit Filtern nach Unternehmen, Datum, Status, Typ, Lieferant
- Detailansicht: Original-Vorschau (PDF/Bild/XML), erkannte Daten mit Herkunft je Feld,
  zugehörige E-Mail, Dubletten, Übertragungen, Freigabe mit Korrekturen, Ignorieren, Dublette, Testlauf
- Prüfung (Dokumente + E-Mails inkl. Abschlussvermerk), DATEV-Versandprotokoll mit Aktionen
- Postfachverwaltung (Testverbindung, Domain, Aktivierung, Jetzt abrufen, Ordner)
- DATEV-Einstellungen: Testmodus, Zieladresse, Absender, Checkliste, Produktivfreigabe, Auto-Versand
- XML-Warteschlange, Einstellungen (inkl. eigene USt-ID), Audit-Log; responsive bis Smartphone
- Im Browser gegen echte Demo-Daten geprüft; dabei behoben: zu strenge E-Mail-Prüfung beim Login,
  Windows-Pfadlänge im lokalen Speicher

## Phase 6 – Produktivbetrieb ✅ (technisch) – Go-Live offen

- Sicherheitsreview mit Abhängigkeits-Scan, Befunde behoben/bewertet (`docs/SECURITY.md`)
- Überwachung mit Alarmen per E-Mail/Telegram, Entwarnung, Wiederholungsfristen; aktive Alarme im Dashboard
- Technische Bereinigung, Aufbewahrungs- und Löschkonzept (steuerrelevante Belege nie automatisch)
- Verschlüsseltes DB-Backup in eigenen Bucket, Rotation, wöchentliche Wiederherstellungsprüfung,
  Integritätsprüfung des Dokumentenspeichers (`docs/BACKUP_RESTORE.md`)
- `invoiceflow ops go-live-check`, Benutzerverwaltung per CLI, Railway-Cron-Konfigurationen
- Go-Live-Checkliste mit Freigabe (`docs/GO_LIVE.md`) – **abzuarbeiten nach dem Deployment**

## Offene Punkte, die nur Sie klären können

| Punkt | Wofür | Status |
|-------|-------|--------|
| ALL-INKL IMAP-Host je Postfach (z. B. `w0xxxxxx.kasserver.com`) | SBC-Abruf | offen |
| IMAP-Benutzernamen/Passwörter aller 5 Postfächer | Abruf | als Railway-Variablen anlegen |
| Domains `mail@` und `sascha.senninger@sbc-symbio.de` bestätigen | Aktivierung | offen |
| Exakte DATEV-Upload-Mail-Adressen aus DATEV (nicht Screenshot) | Versand | offen – siehe Hinweis unten |
| SMTP-Absender je Unternehmen, bei DATEV autorisiert | Versand | offen |
| KI-Anbieter + AV-Vertrag | Phase 3 | offen |
| OCR-Dienst (lokal Tesseract oder extern) | Scan-PDFs | offen |

**Hinweis DATEV-Adressen:** Beide Adressen aus dem Screenshot haben kein
gültiges UUID-Format (Gruppen `8962c-f4235f1` bzw. `ea0-fe82180f6`; eine UUID
endet mit einer 12-stelligen Gruppe). Vermutlich ist beim Abschreiben ein
Bindestrich hineingeraten. Die Adressen sind deshalb nur als *unverifiziert*
hinterlegt; die Anwendung zeigt eine Formatwarnung, bis die Originaladresse
aus DATEV Unternehmen online bestätigt ist.
