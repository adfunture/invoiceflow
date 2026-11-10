"""Betrieb: Überwachung, Wartung, Aufbewahrung, Backup, Go-Live-Prüfung."""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from invoiceflow.api.app import create_app
from invoiceflow.config import AppEnv, Settings, StorageBackend
from invoiceflow.db.enums import EmailMessageStatus, JobStatus, JobType, TransmissionStatus, UserRole
from invoiceflow.db.models import (
    AlertState,
    DatevTransmission,
    Document,
    EmailAttachment,
    EmailMessage,
    LoginAttempt,
    Mailbox,
    ProcessingJob,
    User,
    UserSession,
)
from invoiceflow.db.session import get_sessionmaker
from invoiceflow.jobs import enqueue
from invoiceflow.ops import backup as bk
from invoiceflow.ops.go_live import run_checks
from invoiceflow.ops.maintenance import run_maintenance
from invoiceflow.ops.monitor import collect_findings, configured_notifiers, run_monitor
from invoiceflow.ops.retention import purge_non_invoice_emails, report
from invoiceflow.services import app_settings
from invoiceflow.services.audit import Actor
from invoiceflow.storage import DocumentStore, LocalBackend, get_store
from invoiceflow.workers.base import heartbeat

from .conftest import ApiClient, company_id, make_user
from .samples import invoice_lines, make_pdf
from .test_datev_sender import FakeTransport, configure_destination, ready_invoice, sender
from .test_imap_sync import INFO, accounts  # noqa: F401 – Fixture
from .test_pipeline import PDF, deliver, messages, process

ACTOR = Actor.system("test")
SETTINGS = Settings(app_env=AppEnv.test, cookie_secure=False)


class FakeNotifier:
    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def send(self, subject: str, body: str) -> None:
        if self.fail:
            raise OSError("SMTP nicht erreichbar")
        self.sent.append((subject, body))


def _monitor(notifier, now=None):
    with get_sessionmaker()() as db:
        result = run_monitor(db, SETTINGS, [notifier], now)
        db.commit()
        return result


# --- Überwachung --------------------------------------------------------------------


def test_monitor_alerts_once_then_resolves(accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    from invoiceflow.datev.smtp import SmtpResult
    from invoiceflow.db.enums import AttemptOutcome

    sender(FakeTransport(SmtpResult(AttemptOutcome.unknown, error="timeout"))).run_cycle()
    with get_sessionmaker()() as db:
        heartbeat(db, "imap:x:1", "imap", datetime.now(UTC), {})
        db.execute(update(Mailbox).where(Mailbox.email_address == INFO).values(consecutive_failures=4))
        db.commit()
    notifier = FakeNotifier()
    first = _monitor(notifier, datetime.now(UTC) + timedelta(minutes=30))
    keys = {f.key for f in first.findings}
    assert {"datev:unclear", "worker:imap"} <= keys and any(k.endswith(":failing") for k in keys)
    assert len(notifier.sent) == 1 and "KRITISCH" in notifier.sent[0][0]
    assert "RE-2026-1001" not in notifier.sent[0][1], "keine Rechnungsinhalte im Alarm"

    _monitor(notifier, datetime.now(UTC) + timedelta(minutes=40))
    assert len(notifier.sent) == 1, "kein erneuter Alarm innerhalb der Wiederholungsfrist"

    with get_sessionmaker()() as db:  # Probleme behoben
        db.execute(update(DatevTransmission).values(status=TransmissionStatus.sent_unconfirmed, sent_at=datetime.now(UTC)))
        db.execute(update(Mailbox).values(consecutive_failures=0))
        heartbeat(db, "imap:x:1", "imap", datetime.now(UTC) + timedelta(minutes=50), {})
        db.commit()
    result = _monitor(notifier, datetime.now(UTC) + timedelta(minutes=50))
    assert "datev:unclear" in result.resolved and "Behoben" in notifier.sent[-1][1]


def test_monitor_renotifies_critical_after_six_hours(accounts):
    with get_sessionmaker()() as db:
        enqueue(db, company_id=company_id("adfunture", db), job_type=JobType.process_message, dedup_key="x", payload={})
        db.execute(update(ProcessingJob).values(status=JobStatus.dead))
        db.commit()
    notifier = FakeNotifier()
    now = datetime.now(UTC)
    _monitor(notifier, now)
    _monitor(notifier, now + timedelta(hours=1))
    _monitor(notifier, now + timedelta(hours=7))
    assert len(notifier.sent) == 2


def test_failed_delivery_is_retried_next_run():
    with get_sessionmaker()() as db:
        enqueue(db, company_id=company_id("adfunture", db), job_type=JobType.process_message, dedup_key="x", payload={})
        db.execute(update(ProcessingJob).values(status=JobStatus.dead))
        db.commit()
    result = _monitor(FakeNotifier(fail=True))
    assert result.notification_errors and not result.notified
    ok = FakeNotifier()
    _monitor(ok)
    assert len(ok.sent) == 1


def test_production_expects_all_workers():
    with get_sessionmaker()() as db:
        prod = Settings(app_env=AppEnv.production, cookie_secure=True, storage_backend=StorageBackend.s3)
        keys = {f.key for f in collect_findings(db, prod)}
    assert {"worker:imap", "worker:processing", "worker:datev"} <= keys


def test_notifier_configuration():
    assert configured_notifiers(SETTINGS) == []
    s = Settings(app_env=AppEnv.test, cookie_secure=False, alert_email_to=["it@sbc-symbio.de"], alert_email_from="x@y.de",
                 alert_smtp_host="smtp.strato.de", alert_telegram_chat_id="123", alert_telegram_token_ref="INVOICEFLOW_SECRET_TG")
    assert [n.name for n in configured_notifiers(s)] == ["email", "telegram"]


def test_alerts_endpoint(admin):
    with get_sessionmaker()() as db:
        db.add(AlertState(key="k", severity="critical", message="Test", active=True,
                          first_seen_at=datetime.now(UTC), last_seen_at=datetime.now(UTC)))
        db.commit()
    assert admin.get("/api/dashboard/alerts").json()[0]["message"] == "Test"


# --- Wartung --------------------------------------------------------------------------


def test_maintenance_removes_only_old_technical_rows():
    make_user("a@b.de", UserRole.viewer)
    old = datetime.now(UTC) - timedelta(days=200)
    with get_sessionmaker()() as db:
        user_id = db.scalar(select(User.id))
        db.add(LoginAttempt(email="a@b.de", succeeded=False, attempted_at=old))
        db.add(LoginAttempt(email="a@b.de", succeeded=False))
        db.add(UserSession(user_id=user_id, token_hash="a" * 64, csrf_token="c", expires_at=old))
        db.commit()
        result = run_maintenance(db)
        db.commit()
        assert result["login_attempts"] == 1 and result["user_sessions"] == 1
        assert db.scalar(select(func.count()).select_from(LoginAttempt)) == 1


# --- Aufbewahrung ---------------------------------------------------------------------


def test_purge_only_non_invoice_mails_and_never_documents(accounts):
    shared = make_pdf(["nur ein Anhang ohne Rechnung"])
    deliver(accounts, [("info.pdf", shared, PDF)], subject="Newsletter", body="Hallo")
    deliver(accounts, [("r.pdf", make_pdf(invoice_lines()), PDF)])
    process(accounts)
    with get_sessionmaker()() as db:  # erste Mail als "keine Rechnung" abgeschlossen und alt
        first = db.scalar(select(EmailMessage).order_by(EmailMessage.id))
        db.execute(update(Document).where(Document.email_message_id == first.id).values(status="ignored"))
        db.execute(Document.__table__.delete().where(Document.email_message_id == first.id))
        db.execute(update(EmailMessage).where(EmailMessage.id == first.id).values(
            status=EmailMessageStatus.no_invoice, imported_at=datetime.now(UTC) - timedelta(days=400)))
        db.commit()
        raw_key = first.raw_storage_key
    store = get_store()
    with get_sessionmaker()() as db:
        assert report(db).purge_candidates == 1
        with pytest.raises(PermissionError):
            purge_non_invoice_emails(db, store, ACTOR, apply=True)
        assert purge_non_invoice_emails(db, store, ACTOR, apply=False).messages == 1
        app_settings.set_setting(db, "retention.auto_delete_enabled", True)
        result = purge_non_invoice_emails(db, store, ACTOR, apply=True)
        db.commit()
    assert result.messages == 1 and result.deleted_objects == 2
    assert not store.backend.exists(raw_key)
    msgs = messages()
    assert msgs[0].raw_purged_at is not None and msgs[0].subject == "Newsletter", "Metadaten bleiben"
    with get_sessionmaker()() as db:
        doc = db.scalar(select(Document))
    assert store.get(doc.storage_key, doc.sha256), "Rechnungsoriginale unangetastet"


def test_purge_keeps_objects_shared_with_invoices(accounts):
    pdf = make_pdf(invoice_lines())
    deliver(accounts, [("r.pdf", pdf, PDF)], subject="A")
    deliver(accounts, [("r.pdf", pdf, PDF)], subject="B")
    process(accounts)
    with get_sessionmaker()() as db:  # zweite Mail: Dublette → als "keine Rechnung" markieren, Dokument weg
        second = db.scalars(select(EmailMessage).order_by(EmailMessage.id)).all()[1]
        db.execute(Document.__table__.delete().where(Document.email_message_id == second.id))
        db.execute(update(EmailMessage).where(EmailMessage.id == second.id).values(
            status=EmailMessageStatus.no_invoice, imported_at=datetime.now(UTC) - timedelta(days=400)))
        app_settings.set_setting(db, "retention.auto_delete_enabled", True)
        db.commit()
        result = purge_non_invoice_emails(db, get_store(), ACTOR, apply=True)
        db.commit()
    assert result.kept_shared_objects == 1, "PDF wird von der ersten (Rechnungs-)Mail weiter genutzt"
    with get_sessionmaker()() as db:
        att = db.scalar(select(EmailAttachment).where(EmailAttachment.storage_key.is_not(None)))
    assert get_store().get(att.storage_key, att.sha256) == pdf


# --- Backup ---------------------------------------------------------------------------


KEY = b"b" * 32


def test_backup_encryption_roundtrip_and_tamper_detection(tmp_path):
    src = tmp_path / "dump"
    src.write_bytes(b"x" * (bk.CHUNK + 1234))  # zwei Blöcke
    enc, out = tmp_path / "enc", tmp_path / "out"
    sha = bk.encrypt_file(src, enc, KEY, "db/a.enc")
    assert bk.decrypt_file(enc, out, KEY, "db/a.enc") == sha and out.read_bytes() == src.read_bytes()
    with pytest.raises(bk.BackupError):
        bk.decrypt_file(enc, out, KEY, "db/anderer-name.enc")  # an den Namen gebunden
    with pytest.raises(bk.BackupError):
        bk.decrypt_file(enc, out, b"c" * 32, "db/a.enc")
    data = enc.read_bytes()
    first_block = 4 + 16 + bk.CHUNK + 16
    (tmp_path / "cut").write_bytes(data[:first_block])  # letzter Block abgeschnitten
    with pytest.raises(bk.BackupError, match="unvollständig"):
        bk.decrypt_file(tmp_path / "cut", out, KEY, "db/a.enc")


def _fake_runner(dump_content: bytes = b"PGDMP fake", list_output: str = ""):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[0] == "pg_dump":
            Path(cmd[cmd.index("--file") + 1]).write_bytes(dump_content)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, list_output, "")

    return run, calls


def _backup_settings(tmp_path) -> Settings:
    import base64
    return Settings(app_env=AppEnv.development, cookie_secure=False, backup_local_path=str(tmp_path / "backups"),
                    document_encryption_key=base64.b64encode(KEY).decode(),
                    database_url="postgresql://user:geheim@db.internal:5432/invoiceflow?sslmode=require")


def test_backup_and_restore_check(tmp_path):
    settings = _backup_settings(tmp_path)
    target = bk.get_target(settings)
    runner, calls = _fake_runner()
    result = bk.run_backup(settings, target, runner=runner)
    cmd, kwargs = calls[0]
    assert "geheim" not in " ".join(cmd), "Passwort nie auf der Kommandozeile"
    assert kwargs["env"]["PGPASSWORD"] == "geheim" and kwargs["env"]["PGSSLMODE"] == "require"
    assert result.key.startswith("db/") and b"PGDMP" not in (tmp_path / "backups" / result.key).read_bytes()

    listing = "\n".join(f"{i}; 0 1 TABLE DATA public {t} postgres" for i, t in enumerate(bk.REQUIRED_TABLES))
    runner, _ = _fake_runner(list_output=listing)
    check = bk.restore_check(settings, target, runner=runner)
    assert check.key == result.key and check.missing_tables == []
    runner, _ = _fake_runner(list_output="1; 0 1 TABLE DATA public companies postgres")
    assert "documents" in bk.restore_check(settings, target, runner=runner).missing_tables


def test_backup_rotation_keeps_at_least_seven(tmp_path):
    settings = _backup_settings(tmp_path)
    target = bk.get_target(settings)
    now = datetime.now(UTC)
    for days_ago in range(60, 0, -5):
        runner, _ = _fake_runner()
        bk.run_backup(settings, target, runner=runner, now=now - timedelta(days=days_ago))
    import os
    for key, _ in target.list("db/"):  # Dateizeit auf den Namen setzen
        stamp = datetime.strptime(key.rsplit("-", 1)[-1][:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC).timestamp()
        os.utime(tmp_path / "backups" / key, (stamp, stamp))
    runner, _ = _fake_runner()
    result = bk.run_backup(settings, target, runner=runner, now=now)
    remaining = target.list("db/")
    assert result.deleted_old and len(remaining) >= 7
    assert all(datetime.strptime(k.rsplit("-", 1)[-1][:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC) >= now - timedelta(days=36)
               for k, _ in remaining)


def test_pg_dump_failure_raises(tmp_path):
    def failing(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "connection refused")
    settings = _backup_settings(tmp_path)
    with pytest.raises(bk.BackupError, match="pg_dump"):
        bk.run_backup(settings, bk.get_target(settings), runner=failing)


def test_production_requires_backup_bucket():
    prod = Settings(app_env=AppEnv.production, cookie_secure=True, storage_backend=StorageBackend.s3, backup_local_path="/tmp/x")
    with pytest.raises(bk.BackupError):
        bk.get_target(prod)


def test_verify_storage_detects_tampering(accounts, tmp_path):
    deliver(accounts, [("r.pdf", make_pdf(invoice_lines()), PDF)])
    process(accounts)
    store = get_store()
    with get_sessionmaker()() as db:
        assert bk.verify_storage(db, store).failed == []
        doc = db.scalar(select(Document))
    broken = DocumentStore(LocalBackend(tmp_path), b"z" * 32)  # falscher Schlüssel = nicht lesbar
    (tmp_path / doc.storage_key).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / doc.storage_key).write_bytes(store.backend.read(doc.storage_key))
    with get_sessionmaker()() as db:
        assert bk.verify_storage(db, broken, sample=5).failed


# --- Go-Live und Härtung ------------------------------------------------------------


def test_go_live_check_in_test_environment_blocks():
    with get_sessionmaker()() as db:
        checks = run_checks(db, SETTINGS)
    fails = {c.message for c in checks if c.status == "fail"}
    assert "APP_ENV ist nicht production" in fails
    assert any("Lokaler Teil ist keine UUID" in m for m in fails), "Screenshot-Adressen blockieren"
    assert any(c.area == "Backup" and c.status == "fail" for c in checks)


def test_production_requires_allowed_hosts():
    with pytest.raises(RuntimeError, match="ALLOWED_HOSTS"):
        create_app(Settings(app_env=AppEnv.production, cookie_secure=True, storage_backend=StorageBackend.s3))


def test_host_header_check_allows_railway_healthcheck():
    app = create_app(Settings(app_env=AppEnv.test, cookie_secure=False, allowed_hosts=["invoiceflow.example.de"]))
    client = TestClient(app)
    assert client.get("/health", headers={"host": "invoiceflow.example.de"}).status_code == 200
    assert client.get("/health", headers={"host": "healthcheck.railway.app"}).status_code == 200
    assert client.get("/health", headers={"host": "angreifer.example"}).status_code == 400


def test_cli_deactivate_user_revokes_sessions(monkeypatch):
    from invoiceflow.cli import main

    make_user("weg@adfunture.de", UserRole.reviewer)
    client = ApiClient()
    client.login("weg@adfunture.de")
    assert main(["user", "deactivate", "weg@adfunture.de"]) == 0
    assert client.get("/api/auth/me").status_code == 401
    assert client.login("weg@adfunture.de").status_code == 401


def test_monitor_alerts_on_failed_or_stale_backup():
    from datetime import UTC, datetime, timedelta

    from invoiceflow.services.audit import audit

    now = datetime.now(UTC)
    prod = Settings(app_env=AppEnv.production, cookie_secure=True, storage_backend=StorageBackend.s3)
    with get_sessionmaker()() as db:
        findings = {f.key: f for f in collect_findings(db, prod, now)}
        assert findings["ops:backup"].severity == "critical" and "noch nie" in findings["ops:backup"].message
        assert findings["ops:restore_check"].severity == "warning"
        # Testumgebung: ohne Läufe kein Alarm
        assert "ops:backup" not in {f.key for f in collect_findings(db, SETTINGS, now)}

        audit(db, ACTOR, "ops.backup_completed", details={})
        audit(db, ACTOR, "ops.restore_check_passed", details={})
        db.commit()
        keys = {f.key for f in collect_findings(db, prod, now)}
        assert "ops:backup" not in keys and "ops:restore_check" not in keys
        stale = {f.key: f for f in collect_findings(db, prod, now + timedelta(hours=30))}
        msg = stale["ops:backup"].message
        assert ("vor 29 Stunden" in msg or "vor 30 Stunden" in msg) and "ops:restore_check" not in stale

        audit(db, ACTOR, "ops.backup_failed", details={"error": "pg_dump: server version mismatch"})
        db.commit()
        failed = {f.key: f for f in collect_findings(db, SETTINGS, now)}
        assert failed["ops:backup"].severity == "critical" and "fehlgeschlagen" in failed["ops:backup"].message


def test_cli_backup_failure_is_recorded_for_monitor(monkeypatch):
    from sqlalchemy import select

    from invoiceflow.cli import main
    from invoiceflow.db.models import AuditLog
    from invoiceflow.ops import backup

    def broken(*_a, **_k):
        raise backup.BackupError("pg_dump fehlgeschlagen: server version mismatch")

    monkeypatch.setattr(backup, "run_backup", broken)
    monkeypatch.setattr(backup, "get_target", lambda _s: None)
    with pytest.raises(backup.BackupError):
        main(["ops", "backup"])
    with get_sessionmaker()() as db:
        row = db.scalar(select(AuditLog).where(AuditLog.action == "ops.backup_failed"))
    assert row is not None and "version mismatch" in row.details["error"]


def test_monitor_warns_when_ai_enabled_but_worker_has_no_key(monkeypatch):
    from datetime import UTC, datetime

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with get_sessionmaker()() as db:
        heartbeat(db, "processing:test:1", "processing", datetime.now(UTC), {})
        assert "config:ai_key" not in {f.key for f in collect_findings(db, SETTINGS)}
        app_settings.set_setting(db, "ai.enabled", True)
        app_settings.set_setting(db, "ai.provider", "anthropic")
        db.commit()
        assert "config:ai_key" in {f.key for f in collect_findings(db, SETTINGS)}
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        heartbeat(db, "processing:test:1", "processing", datetime.now(UTC), {})
        assert "config:ai_key" not in {f.key for f in collect_findings(db, SETTINGS)}
