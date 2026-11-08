"""Go-Live-Prüfung: Ist die Installation bereit für den Produktivbetrieb?

Liefert eine Liste von Prüfpunkten (ok / warn / fail). ``fail`` blockiert die
Produktivfreigabe, ``warn`` muss bewusst entschieden werden. Die eigentliche
Freigabe des DATEV-Versands bleibt eine Admin-Handlung im Dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from invoiceflow.config import Settings, StorageBackend
from invoiceflow.db.enums import UserRole
from invoiceflow.db.models import AuditLog, Company, DatevDestination, Mailbox, User, WorkerHeartbeat
from invoiceflow.ops.monitor import WORKER_TYPES, configured_notifiers
from invoiceflow.services import app_settings
from invoiceflow.services.datev_gate import address_format_warnings, missing_steps
from invoiceflow.storage import StorageError, _load_key


@dataclass(frozen=True)
class Check:
    status: str  # ok | warn | fail
    area: str
    message: str


def run_checks(db: Session, settings: Settings, now: datetime | None = None) -> list[Check]:
    now = now or datetime.now(UTC)
    checks: list[Check] = []

    def add(ok: bool, area: str, good: str, bad: str, severity: str = "fail") -> None:
        checks.append(Check("ok" if ok else severity, area, good if ok else bad))

    # Infrastruktur
    add(settings.is_production, "Umgebung", "APP_ENV=production", "APP_ENV ist nicht production")
    add(settings.cookie_secure, "Umgebung", "Sitzungscookie nur über HTTPS", "COOKIE_SECURE ist aus")
    add(settings.allowed_hosts != ["*"], "Umgebung", f"ALLOWED_HOSTS: {', '.join(settings.allowed_hosts)}", "ALLOWED_HOSTS nicht gesetzt")
    add(settings.storage_backend == StorageBackend.s3, "Speicher", "Dokumente im S3-Bucket", "STORAGE_BACKEND ist nicht s3")
    try:
        _load_key(settings.document_encryption_key)
        checks.append(Check("ok", "Speicher", "Verschlüsselungsschlüssel gültig"))
    except StorageError as exc:
        checks.append(Check("fail", "Speicher", str(exc)))
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        from invoiceflow.cli import _alembic_ini

        head = ScriptDirectory.from_config(Config(str(_alembic_ini()))).get_current_head()
        current = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
        add(current == head, "Datenbank", f"Schema aktuell ({head})", f"Schema {current}, erwartet {head}")
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("fail", "Datenbank", f"Schema nicht prüfbar: {type(exc).__name__}"))

    # Betrieb
    add(bool(configured_notifiers(settings)), "Überwachung", "Alarmkanal konfiguriert", "Kein Alarmkanal (E-Mail oder Telegram)")
    last_backup = db.scalar(select(func.max(AuditLog.occurred_at)).where(AuditLog.action == "ops.backup_completed"))
    add(bool(settings.backup_s3_bucket), "Backup", "Backup-Bucket konfiguriert", "BACKUP_S3_BUCKET nicht gesetzt")
    add(last_backup is not None and last_backup > now - timedelta(hours=26), "Backup",
        f"Letztes Backup {last_backup:%d.%m.%Y %H:%M}" if last_backup else "",
        "Kein erfolgreiches Backup in den letzten 26 Stunden")
    last_restore = db.scalar(select(func.max(AuditLog.occurred_at)).where(AuditLog.action == "ops.restore_check_passed"))
    add(last_restore is not None, "Backup", "Wiederherstellungsprobe durchgeführt", "Noch keine Wiederherstellungsprobe", "warn")
    for worker_type in WORKER_TYPES:
        beat = db.scalar(select(func.max(WorkerHeartbeat.last_beat_at)).where(WorkerHeartbeat.worker_type == worker_type))
        add(beat is not None and beat > now - timedelta(minutes=10), "Worker", f"{worker_type} aktiv",
            f"{worker_type} meldet sich nicht")

    # Zugang
    admins = db.scalar(select(func.count()).select_from(User).where(User.role == UserRole.admin, User.is_active.is_(True))) or 0
    add(admins >= 1, "Zugang", f"{admins} aktive(r) Admin(s)", "Kein aktiver Admin")
    if admins == 1:
        checks.append(Check("warn", "Zugang", "Nur ein Admin – Vertretung für Freigaben und Klärungen einrichten"))

    # Fachlich
    for company in db.scalars(select(Company).order_by(Company.id)):
        add(bool(company.vat_id), company.short_name, "Eigene USt-ID hinterlegt",
            "Eigene USt-ID fehlt (wird sonst ggf. als Lieferanten-USt-ID gelesen)", "warn")
        for mb in db.scalars(select(Mailbox).where(Mailbox.company_id == company.id).order_by(Mailbox.email_address)):
            add(mb.is_active, company.short_name, f"Postfach {mb.email_address} aktiv",
                f"Postfach {mb.email_address} nicht aktiv", "warn")
        dest = db.scalar(select(DatevDestination).where(DatevDestination.company_id == company.id))
        if dest is None:
            checks.append(Check("fail", company.short_name, "Kein DATEV-Ziel"))
            continue
        warnings = address_format_warnings(dest.target_address)
        add(not warnings, company.short_name, "DATEV-Adresse plausibel", "; ".join(warnings))
        missing = missing_steps(dest)
        add(not missing, company.short_name, "Freigabe-Checkliste vollständig", "Offen: " + ", ".join(missing))
        add(dest.production_released_at is not None, company.short_name, "Produktivfreigabe erteilt",
            "Produktivfreigabe fehlt (erst nach allen Schritten im Dashboard)", "warn")

    test_mode = bool(app_settings.get_setting(db, "datev.test_mode"))
    checks.append(Check("warn" if test_mode else "ok", "DATEV", "Testmodus aus" if not test_mode else "Testmodus ist an – kein Versand"))
    checks.append(Check("ok" if settings.datev_allow_real_send else "warn", "DATEV",
                        "DATEV_ALLOW_REAL_SEND=true" if settings.datev_allow_real_send else "DATEV_ALLOW_REAL_SEND=false – kein Versand"))
    if app_settings.get_setting(db, "ai.enabled"):
        checks.append(Check("warn", "Datenschutz", "KI-Fallback aktiv – Auftragsverarbeitungsvertrag mit dem Anbieter erforderlich"))
    if app_settings.get_setting(db, "retention.auto_delete_enabled"):
        checks.append(Check("warn", "Aufbewahrung", "Automatische Löschung (nur E-Mails ohne Rechnungsbezug) ist an"))
    return checks
