# InvoiceFlow

Eingangsrechnungen aus den Postfächern von **SBC symbio GmbH & Co. KG** und
**Adfunture Outdoor GmbH** automatisch erkennen, auf Dubletten prüfen und
genau einmal an den richtigen DATEV-Unternehmen-online-Bestand übertragen.

* Architektur: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
* Umsetzungsplan, offene Punkte: [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)
* Railway-Deployment: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
* Sicherheit, Datenschutz, Aufbewahrung: [docs/SECURITY.md](docs/SECURITY.md)
* Backup und Wiederherstellung: [docs/BACKUP_RESTORE.md](docs/BACKUP_RESTORE.md)
* Go-Live-Checkliste: [docs/GO_LIVE.md](docs/GO_LIVE.md)

**Stand: alle sechs Phasen umgesetzt.** Offen sind Deployment und Produktivfreigabe nach
[docs/GO_LIVE.md](docs/GO_LIVE.md). Der automatische DATEV-Versand ist
standardmäßig aus und durch drei unabhängige Sperren gesichert.

## Projektstruktur

```
backend/
  invoiceflow/
    config.py            Umgebungsvariablen, Importstart 08.11.2026 Europe/Berlin
    logging_setup.py     JSON-Logs mit Secret-Maskierung
    db/models.py         Alle Tabellen inkl. mandantensicherer Fremdschlüssel
    db/enums.py          Statusdefinitionen
    security/            Argon2-Passwörter, Sessions, Secret-Referenzen
    services/            Audit, Einstellungen, Postfächer, DATEV-Freigabe
    api/                 FastAPI: Auth, Unternehmen, Postfächer, E-Mails, DATEV, Einstellungen, Health
    mail/                IMAP-Adapter (nur lesend), Abruflogik, MIME-Parser, SSRF-Prüfung
    recognition/         PDF/ZUGFeRD/XRechnung, Regeln, OCR, KI-Fallback, Pipeline mit Dublettenprüfung
    storage.py           AES-256-GCM-verschlüsselter Dokumentenspeicher (lokal/S3)
    jobs.py              Job-Queue in PostgreSQL (SKIP LOCKED, Lease, Backoff)
    datev/               Upload-Mail-Versand (SMTP, Mail-Aufbau, Sender), Eingriffe, XML-Kanal
    workers/             Hintergrund-Worker (imap, processing, datev)
    ops/                 Überwachung/Alarme, Wartung, Aufbewahrung, Backup, Go-Live-Prüfung
    seed.py              Stammdaten beider Unternehmen (idempotent)
    cli.py               invoiceflow migrate | seed | create-user | check-config |
                         worker imap | sync-mailbox | generate-key
  alembic/versions/      Migrationen
  tests/                 pytest gegen echtes PostgreSQL
frontend/                React-Dashboard (siehe frontend/README.md)
railway/*.json           Railway-Konfiguration (web, 3 Worker, 3 Betriebs-Crons)
tools/testdb/            PGlite-Postgres für lokale Tests ohne Docker
```

## Lokal starten

### Mit Docker

```bash
cp .env.example .env
docker compose up --build
docker compose exec web invoiceflow create-user --email admin@sbc-symbio.de --role admin
```

API-Dokumentation: http://localhost:8080/api/docs

### Ohne Docker (Windows/macOS/Linux)

```bash
python -m venv .venv
.venv/Scripts/pip install -e "backend[dev]"      # macOS/Linux: .venv/bin/pip
cd tools/testdb && npm install && npm start      # Postgres 17 (PGlite) auf Port 54329
```

In einem zweiten Terminal:

```bash
cd backend
export DATABASE_URL="postgresql+psycopg://postgres:postgres@127.0.0.1:54329/postgres?sslmode=disable"
export COOKIE_SECURE=false
export DB_DISABLE_PREPARED_STATEMENTS=true   # nur für PGlite nötig
export DOCUMENT_ENCRYPTION_KEY=$(invoiceflow generate-key 2>/dev/null)
invoiceflow migrate && invoiceflow seed
invoiceflow create-user --email admin@sbc-symbio.de --role admin
uvicorn invoiceflow.api.app:app --reload --port 8080
```

IMAP-Worker (drittes Terminal, gleiche Variablen): `invoiceflow worker imap`

## Tests

```bash
cd backend
pytest -q
```

Standardmäßig gegen PGlite auf `127.0.0.1:54329` (siehe oben). Für ein echtes
PostgreSQL `TEST_DATABASE_URL` setzen – **die Test-DB wird geleert**. Tests
schicken nie E-Mails an DATEV; `DATEV_ALLOW_REAL_SEND=true` ist in der
Testumgebung per Konfigurationsprüfung verboten.

Hinweis: PGlite ist ein Ein-Prozess-Postgres. Tests für echte Parallelität
(zwei Worker, `SKIP LOCKED`) laufen deshalb in CI gegen PostgreSQL 17
(`.github/workflows/ci.yml`).

## Sicherheitsprinzipien (Kurzfassung)

* Postfach → Unternehmen ist fest (DB-Trigger), DATEV-Ziel kommt nur aus der Konfiguration.
* Zusammengesetzte Fremdschlüssel verhindern Mandantenvermischung in der Datenbank.
* Postfach- und SMTP-Passwörter nur als Railway-Variablen `INVOICEFLOW_SECRET_*`.
* Doppelversand an DATEV durch partielle Unique-Indizes ausgeschlossen.
* Audit-Log ist per Trigger unveränderlich.
