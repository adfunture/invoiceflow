# Go-Live-Checkliste (Produktivfreigabe)

Technischer Stand vor Go-Live: `invoiceflow ops go-live-check` (in der `railway ssh`-Shell des web-Service)
zeigt **0 blockierend**. Die fachliche Freigabe des DATEV-Versands erfolgt danach je Unternehmen im Dashboard.

## A. Infrastruktur (Railway, Region EU West)

- [ ] Projekt `invoiceflow` mit Postgres, Railway-Backups aktiviert
- [ ] Dokument-Bucket (S3, EU) und **separater** Backup-Bucket (anderer Account/Anbieter empfohlen)
- [ ] Shared Variables: `DOCUMENT_ENCRYPTION_KEY` (offline gesichert!), `S3_*`, `BACKUP_S3_*`, `INVOICEFLOW_SECRET_*`
- [ ] `web`: `APP_ENV=production`, `COOKIE_SECURE=true`, `ALLOWED_HOSTS`, `PUBLIC_BASE_URL`, eigene Domain mit TLS
- [ ] Services `worker-imap`, `worker-processing`, `worker-datev` (je 1 Replika), Cron `ops-monitor`, `ops-backup`, `ops-restore-check`
- [ ] Alarmkanal: `ALERT_EMAIL_*` und/oder `ALERT_TELEGRAM_*`; Testalarm erhalten (z. B. Worker kurz stoppen)
- [ ] Erstes Backup erfolgreich, `invoiceflow ops restore-check` erfolgreich
- [ ] Railway-Zugang nur für berechtigte Personen, 2FA aktiv

## B. Zugang

- [ ] Mindestens zwei Admins (Vertretung), persönliche Konten, Passwort beim ersten Login geändert
- [ ] Prüfer/Leser mit Unternehmenszuordnung angelegt

## C. Postfächer

- [ ] ALL-INKL-IMAP-Host und Logins der drei SBC-Postfächer eingetragen
- [ ] Domains `mail@` und `sascha.senninger@sbc-symbio.de` bestätigt
- [ ] Alle fünf Postfächer: Testverbindung OK, aktiviert, erster Abruf hat seit 08.11.2026 nachgeholt
- [ ] Je Postfach die Ordnerliste geprüft: alle Rechnungsordner „überwacht“, Gesendet/Papierkorb/Spam
      „ausgeschlossen“; bewusst eingeschaltete Ausnahmen dokumentiert
- [ ] Eigene USt-IDs beider Unternehmen hinterlegt (Einstellungen → Unternehmen)

## D. Erkennung (Stichprobe der ersten Tage)

- [ ] 20 importierte Belege je Unternehmen gegen das Original geprüft (Nummer, Datum, Brutto, Lieferant)
- [ ] Zuordnung: Testrechnung an das jeweils andere Unternehmen landet in „Prüfung nötig“
- [ ] Dublette: gleiche Rechnung an zwei Postfächer → genau eine „Bereit für DATEV“
- [ ] Entscheidung zu OCR (`ocr.provider`) und KI-Fallback (nur mit AV-Vertrag) dokumentiert

## E. DATEV je Unternehmen (Dashboard → DATEV-Einstellungen)

1. [ ] Upload-Mail-Adresse **aus DATEV Unternehmen online kopiert** (nicht Screenshot), verifiziert – keine Formatwarnung
2. [ ] SMTP-Absender eingetragen und in DATEV als berechtigter Absender hinterlegt
3. [ ] Unternehmenszuordnung getestet (D)
4. [ ] Dublettenprüfung getestet (D)
5. [ ] Testlauf (ohne Versand) für einen Beleg geprüft
6. [ ] `DATEV_ALLOW_REAL_SEND=true`, Testmodus aus, 2–3 **Testlieferungen** gesendet und in DATEV sichtbar → bestätigt
7. [ ] Entscheidung zu ZUGFeRD per Upload Mail und Mailgröße mit Steuerberater/DATEV geklärt
8. [ ] **Produktivfreigabe** („PRODUKTIV FREIGEBEN“) durch Admin, danach Auto-Versand an

## F. Organisation

- [ ] Zuständigkeit: Wer bearbeitet „Prüfung nötig“ und unklare Versände (täglich)?
- [ ] Wer reagiert auf Alarme, wer vertritt?
- [ ] Aufbewahrungsfristen mit Steuerberater abgestimmt (`retention.*`)
- [ ] AV-Verträge (Railway, Backup-Anbieter, ggf. Anthropic) abgelegt
- [ ] Notfallablauf aus `docs/BACKUP_RESTORE.md` bekannt

## Freigabe

| Rolle | Name | Datum | Unterschrift |
|---|---|---|---|
| Geschäftsführung SBC symbio | | | |
| Geschäftsführung Adfunture | | | |
| Buchhaltung / Steuerberatung | | | |
