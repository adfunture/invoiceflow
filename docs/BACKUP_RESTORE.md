# Backup und Wiederherstellung

## Was gesichert wird

| Ebene | Wie | Wann | Aufbewahrung |
|---|---|---|---|
| PostgreSQL (Railway) | Railway-Backups im Postgres-Service aktivieren | täglich | mind. 30 Tage |
| PostgreSQL (eigenes Backup) | `invoiceflow ops backup`: `pg_dump` → AES-256-GCM → eigener Bucket | täglich 01:30 UTC (`railway/ops-backup.json`) | `BACKUP_KEEP_DAYS` (35), mind. 7 |
| Originaldokumente | liegen verschlüsselt im Dokument-Bucket; Versionierung/Replikation des Buckets aktivieren, falls der Anbieter es bietet | laufend | unbegrenzt |
| Schlüssel | `DOCUMENT_ENCRYPTION_KEY` (+ ggf. `BACKUP_ENCRYPTION_KEY`) **offline** im Passwortmanager beider Geschäftsführungen | einmalig | dauerhaft |

**Ohne Schlüssel sind Dokumente und Backups wertlos.** Den Schlüssel nie nur in Railway halten.

Das eigene Backup sollte in einem **anderen Account/Anbieter** liegen als das Railway-Projekt
(Schutz gegen Verlust oder Kompromittierung des Railway-Accounts).

## Automatische Prüfungen

* täglich nach dem Backup: `invoiceflow ops verify-storage --sample 50` – 50 zufällige Originale entschlüsseln, Hash prüfen
* wöchentlich (So 04:00 UTC, `railway/ops-restore-check.json`): `invoiceflow ops restore-check` – neuestes Backup
  laden, entschlüsseln, `pg_restore --list`, Pflichttabellen prüfen
* Erfolg/Misserfolg steht im Audit-Log (`ops.backup_completed`, `ops.restore_check_passed`, `ops.storage_verified`);
  die Go-Live-Prüfung verlangt ein Backup der letzten 26 Stunden

## Wiederherstellung (Notfall)

1. **Schreibende Dienste stoppen**: `worker-imap`, `worker-processing`, `worker-datev` in Railway auf 0 Replikas.
   Vor allem `worker-datev`, damit nichts doppelt an DATEV geht.
2. **Neue, leere Datenbank** anlegen (neuer Railway-Postgres-Service oder bestehende leeren – nur wenn sicher).
3. Backup holen und entschlüsseln (in einer `railway ssh`-Shell oder lokal mit gleichen Variablen):

   ```bash
   invoiceflow ops restore-check            # prüft und zeigt den Key des neuesten Backups
   python - <<'EOF'
   from pathlib import Path
   from invoiceflow.config import get_settings
   from invoiceflow.ops.backup import backup_key, decrypt_file, get_target
   s = get_settings(); key = "db/2026/11/invoiceflow-20261108T013000Z.dump.enc"   # anpassen
   get_target(s).download(key, Path("/tmp/db.enc"))
   decrypt_file(Path("/tmp/db.enc"), Path("/tmp/db.dump"), backup_key(s), key)
   EOF
   ```

4. Einspielen:

   ```bash
   pg_restore --no-owner --no-privileges --dbname "$NEUE_DATABASE_URL" /tmp/db.dump
   rm /tmp/db.enc /tmp/db.dump
   ```

5. `DATABASE_URL` aller Services auf die neue Datenbank setzen, `web` deployen (führt `migrate` aus),
   `invoiceflow ops go-live-check` und `invoiceflow ops verify-storage` laufen lassen.
6. **DATEV-Abgleich vor dem Neustart von `worker-datev`:** Übertragungen, die nach dem Backup-Zeitpunkt gesendet
   wurden, fehlen in der wiederhergestellten Datenbank. Im DATEV-Bestand nachsehen, welche Belege nach dem
   Backup-Zeitpunkt eingegangen sind, und die betroffenen Dokumente im Dashboard als „ignoriert“ (bereits in DATEV)
   markieren, bevor der Versand wieder läuft.
7. Worker wieder starten; IMAP holt alles seit dem letzten gespeicherten Stand nach (Duplikate werden erkannt).

## Wiederherstellungsprobe

Mindestens **halbjährlich** eine vollständige Probe in eine Wegwerf-Datenbank (Schritte 3–5 ohne Umschalten)
und das Ergebnis im Betriebshandbuch vermerken. Die wöchentliche automatische Prüfung ersetzt das nicht.
