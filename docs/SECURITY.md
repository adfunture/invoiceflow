# Sicherheits- und Datenschutzkonzept

Stand: 08.11.2026 (Phase 6). Gilt für SBC symbio GmbH & Co. KG und Adfunture Outdoor GmbH.

## 1. Schutzziele und Bedrohungen

| Bedrohung | Auswirkung | Gegenmaßnahme |
|---|---|---|
| Beleg wird dem falschen Unternehmen zugeordnet | falsche Buchhaltung | Postfach → Unternehmen fest (DB-Trigger), zusammengesetzte Fremdschlüssel, Empfängerprüfung → manuelle Prüfung |
| Doppelte Übertragung an DATEV | Doppelbuchung | Unique-Indizes je Datei/Rechnung/Dokument, `SKIP LOCKED`, unklarer Versand → nie automatisch wiederholt |
| Manipulierte E-Mail/Rechnung steuert das System (Prompt Injection) | falsches Ziel, falsche Daten | Zieladresse nur aus Konfiguration; KI-Schema ohne Unternehmen/Ziel/Status; KI-Werte müssen im Dokument stehen |
| Schadhafte Anhänge (Makros, EXE, XXE, Zip-Bomben) | Codeausführung, DoS | Typ am Inhalt, Blockliste, Größenlimits, defusedxml, ZIP nie entpackt, keine Ausführung |
| SSRF über Rechnungslinks | Zugriff auf interne Dienste | Links werden nie automatisch geladen; Prüfung auf öffentliche IPs für späteren manuellen Abruf |
| Kontoübernahme im Dashboard | Freigaben, Datenabfluss | Argon2id, Sperre nach Fehlversuchen, HttpOnly/Secure/SameSite=Strict, CSRF-Header, Leerlauf-Ablauf, Rollen |
| Datenabfluss aus Speicher/Backup | Vertraulichkeit | AES-256-GCM je Datei (an Speicherpfad gebunden), Backups verschlüsselt, Schlüssel nur in Railway-Variablen |
| Zugangsdaten in Datenbank/Logs | Kompromittierung der Postfächer | Passwörter nur als `INVOICEFLOW_SECRET_*`-Variablen; Log-Maskierung |
| Unbemerkte Ausfälle | verspätete Buchhaltung | Heartbeats, Überwachung alle 10 min, Alarm per E-Mail/Telegram |
| Datenverlust | GoBD-Verstoß | Originale unveränderlich, tägliches Backup, wöchentliche Wiederherstellungsprobe, Speicherprüfung |

## 2. Technische Maßnahmen

**Transport:** Railway terminiert TLS; die App lehnt in Produktion `X-Forwarded-Proto: http` ab und setzt HSTS.
IMAP/SMTP nur mit TLS und Zertifikatsprüfung.

**Anwendung:** CSP (`default-src 'self'`, keine fremden Skripte, `frame-ancestors 'none'`),
`X-Content-Type-Options`, `Referrer-Policy`, Host-Header-Prüfung (`ALLOWED_HOSTS`, Pflicht in Produktion),
API-Doku in Produktion abgeschaltet, Rate Limit je IP, Eingaben mit `extra="forbid"`, ORM statt SQL-Strings.
Original-Downloads mit `Content-Security-Policy: sandbox`.

**Zugriff:** Rollen `admin` (Konfiguration, Freigaben, Klärungen), `reviewer` (Prüfen, Senden, Bestätigen),
`viewer` (lesen). Nicht-Admins sehen nur zugewiesene Unternehmen (fremde Daten → 404).
Benutzerverwaltung per CLI (`create-user`, `user deactivate|activate|reset-password`), Sitzungen werden dabei beendet.

**Nachvollziehbarkeit:** Audit-Log für alle Änderungen, Freigaben, Downloads, Versände; per Trigger unveränderlich.

## 3. Ergebnis des Sicherheitsreviews (Phase 6)

| Befund | Bewertung | Maßnahme |
|---|---|---|
| `ALLOWED_HOSTS` war optional | mittel (Host-Header-Angriffe) | in Produktion Pflicht; Railway-Healthcheck-Host automatisch erlaubt |
| Login-Adressprüfung zu streng (interne Domains) | niedrig | auf Syntaxprüfung reduziert |
| Ungültige IP in `X-Forwarded-For` → Serverfehler | mittel | IP wird validiert (Phase 1 behoben) |
| React Router < 7.18: Open Redirect via Backslash in `<Link>`, SSR-Hydration | nicht ausnutzbar | Dashboard nutzt nur feste interne Linkziele und kein SSR; Upgrade auf v7 geplant |
| Vite/esbuild Dev-Server | nur Entwicklung | auf 6.4.5 aktualisiert |
| Python-Abhängigkeiten (pip-audit) | – | keine bekannten Schwachstellen |

**Akzeptierte Restrisiken**

* *Kontosperre als DoS:* 5 Fehlversuche sperren ein Konto 15 Minuten. Bewusst in Kauf genommen.
* *IP-Ermittlung:* Es wird der rechte Eintrag aus `X-Forwarded-For` verwendet (von Railway gesetzt). Ändert Railway
  das Verhalten, greift weiterhin die kontobezogene Sperre.
* *Rechenintensive PDFs:* pypdf hat kein hartes Zeitlimit. Größenlimits begrenzen das; hängt ein Worker,
  meldet die Überwachung den fehlenden Heartbeat, der Job geht nach Lease-Ablauf an einen anderen Worker.
* *Schlüsselrotation:* Der Dokumentschlüssel kann nicht ohne Neuverschlüsselung gewechselt werden.
  Das Format enthält eine Versionskennung; eine Rotation ist nachrüstbar.
* *Datenbank-Admin:* Wer Owner-Rechte auf PostgreSQL hat, kann Trigger entfernen. Zugang zu Railway auf
  wenige Personen mit 2FA beschränken.

## 4. Datenschutz (DSGVO)

| Auftragsverarbeiter | Daten | Erforderlich |
|---|---|---|
| Railway (Hosting, Postgres, ggf. Bucket) | alle | AV-Vertrag / DPA von Railway, Region EU West |
| S3-Anbieter für Backups (falls nicht Railway) | verschlüsselte Backups | AV-Vertrag, EU-Standort |
| ALL-INKL, STRATO | E-Mails (bereits heute) | bestehende Verträge |
| DATEV | Belege | bestehender Vertrag (Steuerberater/DATEV) |
| Anthropic (nur wenn KI-Fallback aktiviert) | Text unklarer Belege | AV-Vertrag **vor** Aktivierung; Standard: aus |

Personenbezogene Daten: Absender/Empfänger von E-Mails, Namen auf Rechnungen, IBAN. Zweck: Erfüllung
steuerlicher Aufbewahrungs- und Buchungspflichten (Art. 6 Abs. 1 lit. c DSGVO). Zugriff nur für
Buchhaltung/Geschäftsführung.

## 5. Aufbewahrung und Löschung

| Daten | Aufbewahrung | Löschung |
|---|---|---|
| Belege (PDF/XML), Rechnungsdaten, zugehörige E-Mails, DATEV-Protokolle, Audit-Log | `retention.original_documents_years` (Standard 10, mind. 8 Jahre für Buchungsbelege, § 147 AO), Frist ab Ende des Kalenderjahres | **nie automatisch**; Bericht `invoiceflow ops retention-report`, Löschung nur nach Entscheidung mit dem Steuerberater |
| E-Mails ohne Rechnungsbezug (`no_invoice`) | `retention.non_invoice_emails_days` (Standard 365) | Rohdaten/Anhänge nur wenn `retention.auto_delete_enabled` an; Metadaten bleiben; geteilte Dateien bleiben |
| Anmeldeversuche | 90 Tage | automatisch (`ops maintenance`) |
| Sitzungen | 30 Tage nach Ablauf | automatisch |
| erfolgreiche Verarbeitungsjobs | 30 Tage | automatisch |
| Datenbank-Backups | `BACKUP_KEEP_DAYS` (Standard 35, mind. 7 Stück) | automatisch |

Hinweis: Die Fristen sind mit dem Steuerberater abzustimmen. Eine Löschpflicht nach DSGVO tritt hinter
die gesetzliche Aufbewahrungspflicht zurück (Art. 17 Abs. 3 lit. b DSGVO).
