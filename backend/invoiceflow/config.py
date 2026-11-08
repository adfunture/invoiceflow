"""Zentrale Konfiguration aus Umgebungsvariablen (Railway Variables / .env).

Geschäftskonfiguration (Unternehmen, Postfächer, DATEV-Ziele) liegt in der
Datenbank. Hier stehen nur Infrastruktur-Parameter und Sicherheitsschalter.
"""
from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Nur Umgebungsvariablen mit diesem Präfix dürfen als Zugangsdaten-Referenz
# (mailboxes.credential_ref, datev_destinations.smtp_credential_ref) aufgelöst
# werden. So lässt sich über die Postfach-Konfiguration nicht z. B.
# DATABASE_URL auslesen.
SECRET_ENV_PREFIX = "INVOICEFLOW_SECRET_"


class AppEnv(StrEnum):
    development = "development"
    test = "test"
    production = "production"


class StorageBackend(StrEnum):
    local = "local"  # Entwicklung/Tests; auf Railway nicht dauerhaft
    s3 = "s3"  # Railway Bucket oder anderer S3-kompatibler Speicher (EU)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: AppEnv = AppEnv.development
    database_url: str = "postgresql+psycopg://invoiceflow:invoiceflow@localhost:5432/invoiceflow"
    db_pool_size: int = 5
    db_max_overflow: int = 5
    # Für PgBouncer (Transaction-Pooling) und PGlite: keine server-seitigen Prepared Statements.
    db_disable_prepared_statements: bool = False
    log_level: str = "INFO"

    # Import ab diesem Zeitpunkt (inklusive), lokale Zeit in import_timezone.
    import_start_date: date = date(2026, 11, 8)
    import_timezone: str = "Europe/Berlin"
    imap_poll_interval_seconds: int = 300

    # Sessions
    session_absolute_hours: int = 12
    session_idle_minutes: int = 60
    cookie_secure: bool = True
    session_cookie_name: str = "invoiceflow_session"

    # Brute-Force-Schutz
    login_max_failures_per_account: int = 5
    login_max_failures_per_ip: int = 20
    login_window_minutes: int = 15

    # Allgemeines Rate Limit pro Client-IP (Token Bucket, pro Web-Instanz)
    rate_limit_per_minute: int = 300

    # Harte Sendesperre auf Infrastrukturebene. Auch wenn im Dashboard alles
    # freigegeben ist, geht ohne DATEV_ALLOW_REAL_SEND=true keine Mail an DATEV.
    datev_allow_real_send: bool = False

    # Dokumentenspeicher. Originale werden AES-256-GCM-verschlüsselt abgelegt.
    # Schlüssel: 32 Byte, base64 (invoiceflow generate-key). Getrennt sichern!
    document_encryption_key: str | None = None
    storage_backend: StorageBackend = StorageBackend.local
    storage_local_path: str = "./data/storage"
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None

    # IMAP
    imap_timeout_seconds: int = 60
    imap_connect_retries: int = 3
    imap_fetch_batch_size: int = 50
    mailbox_sync_lease_seconds: int = 900

    # Alarme (Überwachung). Mindestens ein Kanal sollte in Produktion konfiguriert sein.
    public_base_url: str | None = None  # für Links in Alarmen, z. B. https://invoiceflow.example.de
    alert_email_to: list[str] = Field(default_factory=list)
    alert_email_from: str | None = None
    alert_smtp_host: str | None = None
    alert_smtp_port: int = 465
    alert_smtp_tls_mode: str = "ssl"
    alert_smtp_username: str | None = None
    alert_smtp_credential_ref: str | None = None
    alert_telegram_chat_id: str | None = None
    alert_telegram_token_ref: str | None = None

    # Datenbank-Backup (verschlüsselt) in einen eigenen S3-Bucket – getrennt vom Dokumentenspeicher.
    backup_s3_bucket: str | None = None
    backup_s3_endpoint_url: str | None = None
    backup_s3_region: str | None = None
    backup_s3_access_key_id: str | None = None
    backup_s3_secret_access_key: str | None = None
    backup_local_path: str | None = None  # nur Entwicklung
    backup_encryption_key: str | None = None  # Standard: DOCUMENT_ENCRYPTION_KEY
    backup_keep_days: int = 35

    # Hosts, unter denen das Dashboard erreichbar ist (Host-Header-Prüfung).
    allowed_hosts: list[str] = Field(default_factory=lambda: ["*"])

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, value: str) -> str:
        # Railway liefert postgres:// bzw. postgresql://; SQLAlchemy braucht den Treiber.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix):]
        return value

    @field_validator("import_timezone")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def _production_guards(self) -> "Settings":
        if self.app_env == AppEnv.production and not self.cookie_secure:
            raise ValueError("COOKIE_SECURE muss in Produktion true sein")
        if self.app_env == AppEnv.production and self.storage_backend != StorageBackend.s3:
            raise ValueError("In Produktion muss STORAGE_BACKEND=s3 sein (Container-Dateisystem ist nicht dauerhaft)")
        if self.app_env == AppEnv.test and self.datev_allow_real_send:
            raise ValueError("DATEV_ALLOW_REAL_SEND ist in der Testumgebung verboten")
        return self

    @property
    def import_start_at(self) -> datetime:
        """Startzeitpunkt als zeitzonenbewusstes datetime (08.11.2026 00:00 Europe/Berlin)."""
        return datetime.combine(self.import_start_date, time.min, tzinfo=ZoneInfo(self.import_timezone))

    @property
    def is_production(self) -> bool:
        return self.app_env == AppEnv.production


@lru_cache
def get_settings() -> Settings:
    return Settings()
