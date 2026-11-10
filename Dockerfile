# InvoiceFlow – ein Image für Web und Worker; der Start-Befehl kommt aus der Railway-Konfiguration.

# --- Dashboard bauen ------------------------------------------------------------
FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- Laufzeit -------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Tesseract für lokale OCR von Scan-PDFs (Daten verlassen den Container nicht) und
# PostgreSQL-18-Client aus dem offiziellen PGDG-Repository für pg_dump/pg_restore
# (Debian liefert nur Version 15). Die Major-Version muss >= der des Railway-Servers sein
# (aktuell 18.x), sonst bricht pg_dump mit "server version mismatch" ab.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 invoiceflow
WORKDIR /app/backend

COPY backend/pyproject.toml ./
COPY backend/invoiceflow ./invoiceflow
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
# Editable, damit alembic.ini und /app/frontend/dist relativ zum Paket gefunden werden.
RUN pip install -e ".[ocr]"
COPY --from=frontend /build/dist /app/frontend/dist
RUN chown -R invoiceflow:invoiceflow /app

USER invoiceflow
EXPOSE 8080

CMD ["sh", "-c", "uvicorn invoiceflow.api.app:app --host 0.0.0.0 --port ${PORT:-8080} --no-server-header"]
