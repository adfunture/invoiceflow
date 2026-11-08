"""IMAP-Abruf: Startdatum, Idempotenz, Wiederanlauf, Fehlerverhalten.

Nummern in Klammern verweisen auf die Testfälle aus dem Masterprompt (Abschnitt 14).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select, update

from invoiceflow.config import get_settings
from invoiceflow.db.enums import EmailMessageStatus, JobType
from invoiceflow.db.models import AuditLog, EmailAttachment, EmailMessage, Mailbox, MailboxSyncState, ProcessingJob
from invoiceflow.db.session import get_sessionmaker
from invoiceflow.mail.sync import MailboxSyncer
from invoiceflow.services import app_settings
from invoiceflow.storage import get_store
from invoiceflow.workers.imap_worker import run_once

from .fake_imap import FakeAccount, build_email, factory_for, pdf

BERLIN = ZoneInfo("Europe/Berlin")
INFO, INVOICE = "info@adfunture.de", "invoice@adfunture.de"


@pytest.fixture
def accounts(monkeypatch) -> dict[str, FakeAccount]:
    monkeypatch.setenv("INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO", "richtig")
    monkeypatch.setenv("INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INVOICE", "richtig")
    with get_sessionmaker()() as s:
        s.execute(update(Mailbox).where(Mailbox.email_address.in_([INFO, INVOICE])).values(is_active=True))
        s.commit()
    return {INFO: FakeAccount(), INVOICE: FakeAccount()}


def syncer(accounts, worker_id: str = "test-worker") -> MailboxSyncer:
    return MailboxSyncer(get_sessionmaker(), get_store(), get_settings(), factory_for(accounts), worker_id, sleep=lambda _: None)


def mailbox_id(email: str) -> int:
    with get_sessionmaker()() as s:
        return s.scalar(select(Mailbox.id).where(Mailbox.email_address == email))


def sync(accounts, email: str = INFO, worker_id: str = "test-worker"):
    return syncer(accounts, worker_id).sync_mailbox(mailbox_id(email))


def count(model, *where) -> int:
    with get_sessionmaker()() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def messages(email: str = INFO) -> list[EmailMessage]:
    with get_sessionmaker()() as s:
        return list(s.scalars(select(EmailMessage).where(EmailMessage.mailbox_id == mailbox_id(email)).order_by(EmailMessage.uid)))


def inbox_state(email: str = INFO) -> MailboxSyncState:
    with get_sessionmaker()() as s:
        return s.scalar(select(MailboxSyncState).where(MailboxSyncState.mailbox_id == mailbox_id(email),
                                                       MailboxSyncState.folder_name == "INBOX"))


ON_START = datetime(2026, 11, 8, 0, 0, tzinfo=BERLIN)


# --- Startdatum ---------------------------------------------------------------


def test_20_message_from_8_november_is_imported(accounts):
    accounts[INFO].deliver(build_email("Rechnung 1"), ON_START)
    result = sync(accounts)
    assert result.status == "ok" and result.imported == 1
    assert messages()[0].received_at == ON_START


def test_19_message_from_7_november_is_not_imported(accounts):
    accounts[INFO].deliver(build_email("alt"), datetime(2026, 11, 7, 23, 59, 59, tzinfo=BERLIN))
    accounts[INFO].deliver(build_email("alt UTC"), datetime(2026, 11, 7, 12, 0, tzinfo=UTC))
    result = sync(accounts)
    assert result.imported == 0 and result.folders[0].before_start == 2
    assert accounts[INFO].raw_fetches == [], "Alte Nachrichten werden nicht einmal heruntergeladen"


def test_utc_late_evening_7_nov_is_8_nov_in_berlin(accounts):
    accounts[INFO].deliver(build_email("grenzfall"), datetime(2026, 11, 7, 23, 30, tzinfo=UTC))  # 00:30 Berlin
    assert sync(accounts).imported == 1


def test_forged_date_header_does_not_bypass_start(accounts):
    raw = build_email("gefälscht", date_header=datetime(2026, 12, 1, tzinfo=UTC))
    accounts[INFO].deliver(raw, datetime(2026, 11, 6, 8, 0, tzinfo=BERLIN))
    assert sync(accounts).imported == 0


def test_backfill_catches_up_everything_since_start(accounts):
    """Zwischen Startdatum und erstem Deploy eingegangene Mails werden nachgeholt."""
    for day in range(0, 5):
        accounts[INFO].deliver(build_email(f"Rechnung Tag {day}", body=f"Tag {day}"), ON_START + timedelta(days=day, hours=8))
    assert sync(accounts).imported == 5
    state = inbox_state()
    assert state.backfill_completed_at is not None and state.last_seen_uid == 5


# --- Idempotenz -----------------------------------------------------------------


def test_4_repeated_sync_imports_nothing_twice(accounts):
    accounts[INFO].deliver(build_email("Rechnung", attachments=[("r.pdf", pdf("a"), "application/pdf")]), ON_START + timedelta(hours=1))
    assert sync(accounts).imported == 1
    for _ in range(3):
        assert sync(accounts).imported == 0
    assert count(EmailMessage) == 1
    assert count(EmailAttachment) == 1
    assert count(ProcessingJob, ProcessingJob.job_type == JobType.process_message) == 1


def test_lost_progress_does_not_duplicate(accounts):
    accounts[INFO].deliver(build_email("Rechnung"), ON_START + timedelta(hours=1))
    sync(accounts)
    with get_sessionmaker()() as s:  # Fortschritt geht verloren (z. B. manuell zurückgesetzt)
        s.execute(update(MailboxSyncState).values(last_seen_uid=None, backfill_completed_at=None))
        s.commit()
    assert sync(accounts).imported == 0
    assert count(EmailMessage) == 1


def test_uidvalidity_change_triggers_rescan_without_duplicates(accounts):
    accounts[INFO].deliver(build_email("A", body="a"), ON_START + timedelta(hours=1))
    accounts[INFO].deliver(build_email("B", body="b"), ON_START + timedelta(hours=2))
    sync(accounts)
    accounts[INFO].renumber(new_uidvalidity=2000)
    accounts[INFO].deliver(build_email("C", body="c"), ON_START + timedelta(hours=3))
    result = sync(accounts)
    assert result.folders[0].uidvalidity_changed is True
    assert result.imported == 1
    assert [m.subject for m in messages()] == ["A", "B", "C"]
    assert count(AuditLog, AuditLog.action == "mailbox.uidvalidity_changed") == 1


def test_new_mail_after_first_sync_is_picked_up(accounts):
    accounts[INFO].deliver(build_email("1", body="1"), ON_START + timedelta(hours=1))
    sync(accounts)
    accounts[INFO].deliver(build_email("2", body="2"), ON_START + timedelta(hours=2))
    assert sync(accounts).imported == 1
    assert inbox_state().last_seen_uid == 2


def test_same_mail_in_two_mailboxes_is_imported_in_both(accounts):
    """Dubletten-Entscheidung erfolgt in Phase 3; beide Quellen bleiben protokolliert."""
    raw = build_email("Rechnung", attachments=[("r.pdf", pdf("gleich"), "application/pdf")])
    accounts[INFO].deliver(raw, ON_START + timedelta(hours=1))
    accounts[INVOICE].deliver(raw, ON_START + timedelta(hours=1))
    sync(accounts, INFO)
    sync(accounts, INVOICE)
    assert count(EmailMessage) == 2
    with get_sessionmaker()() as s:
        keys = set(s.scalars(select(EmailAttachment.storage_key)))
    assert len(keys) == 1, "Inhaltsadressiert: dieselbe Datei wird nur einmal gespeichert"


# --- Inhalte --------------------------------------------------------------------


def test_two_attachments_are_stored_separately_and_encrypted(accounts):
    raw = build_email("Zwei Rechnungen", attachments=[
        ("RE-1.pdf", pdf("eins"), "application/pdf"), ("RE-2.pdf", pdf("zwei"), "application/pdf"),
    ])
    accounts[INFO].deliver(raw, ON_START + timedelta(hours=1))
    sync(accounts)
    with get_sessionmaker()() as s:
        atts = list(s.scalars(select(EmailAttachment).order_by(EmailAttachment.part_index)))
    assert [a.filename for a in atts] == ["RE-1.pdf", "RE-2.pdf"]
    assert all(a.safety_status == "ok" and a.storage_key for a in atts)
    store = get_store()
    blob = store.backend.read(atts[0].storage_key)
    assert b"%PDF" not in blob, "verschlüsselt abgelegt"
    assert store.get(atts[0].storage_key, atts[0].sha256) == pdf("eins")
    msg = messages()[0]
    assert store.get(msg.raw_storage_key, msg.raw_sha256) == raw


def test_mail_without_attachment_but_invoice_link_is_kept(accounts):
    raw = build_email("Ihre Rechnung ist verfügbar", body="Rechnung herunterladen: https://portal.example.com/invoice/123",
                      attachments=[])
    accounts[INFO].deliver(raw, ON_START + timedelta(hours=1))
    sync(accounts)
    msg = messages()[0]
    assert msg.attachment_count == 0
    assert msg.detected_invoice_links == ["https://portal.example.com/invoice/123"]
    assert msg.body_mentions_invoice is True
    assert count(ProcessingJob) == 1, "wird zur Prüfung weitergereicht, nicht verworfen"


def test_oversized_message_is_flagged_not_downloaded(accounts):
    with get_sessionmaker()() as s:
        app_settings.set_setting(s, "imap.max_message_size_mb", 1)
        s.commit()
    accounts[INFO].deliver(build_email("groß", attachments=[("x.pdf", pdf("x") + b"0" * 1_100_000, "application/pdf")]),
                           ON_START + timedelta(hours=1))
    sync(accounts)
    msg = messages()[0]
    assert msg.status == EmailMessageStatus.manual_review
    assert msg.raw_storage_key is None and "größer" in msg.review_reason
    assert accounts[INFO].raw_fetches == []


# --- Fehler und Wiederanlauf --------------------------------------------------


def test_10_connection_failure_is_retried_then_recorded(accounts):
    accounts[INFO].deliver(build_email("R"), ON_START + timedelta(hours=1))
    accounts[INFO].fail_connects = 2
    assert sync(accounts).status == "ok", "zwei Fehlversuche, dritter klappt"
    assert accounts[INFO].connects == 3

    accounts[INFO].deliver(build_email("R2", body="2"), ON_START + timedelta(hours=2))
    accounts[INFO].fail_connects = 10
    result = sync(accounts)
    assert result.status == "failed" and "Verbindung abgelehnt" in result.error
    with get_sessionmaker()() as s:
        mb = s.get(Mailbox, mailbox_id(INFO))
        assert mb.consecutive_failures == 1 and mb.last_error and mb.sync_locked_by is None
    accounts[INFO].fail_connects = 0
    assert sync(accounts).imported == 1, "nach Wiederherstellung wird nachgeholt"
    with get_sessionmaker()() as s:
        assert s.get(Mailbox, mailbox_id(INFO)).consecutive_failures == 0


def test_wrong_password_reports_error_without_secret(accounts, monkeypatch):
    monkeypatch.setenv("INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO", "falsch-geheim")
    result = sync(accounts)
    assert result.status == "failed"
    assert "falsch-geheim" not in result.error


def test_missing_secret_fails_cleanly(accounts, monkeypatch):
    monkeypatch.delenv("INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO")
    result = sync(accounts)
    assert result.status == "failed" and "INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO" in result.error


def test_11_interrupted_run_resumes_after_last_complete_message(accounts):
    for i in range(1, 6):
        accounts[INFO].deliver(build_email(f"R{i}", body=str(i)), ON_START + timedelta(hours=i))
    accounts[INFO].fail_fetch_uids = {3}
    result = sync(accounts)
    assert result.status == "failed"
    assert [m.uid for m in messages()] == [1, 2]
    assert inbox_state().last_seen_uid == 2
    accounts[INFO].fail_fetch_uids = set()
    assert sync(accounts).imported == 3
    assert [m.uid for m in messages()] == [1, 2, 3, 4, 5]


def test_11_crashed_worker_lease_expires_and_other_worker_takes_over(accounts):
    accounts[INFO].deliver(build_email("R"), ON_START + timedelta(hours=1))
    with get_sessionmaker()() as s:  # Worker A ist mitten im Abruf gestorben
        s.execute(update(Mailbox).where(Mailbox.id == mailbox_id(INFO))
                  .values(sync_locked_by="worker-a", sync_lease_until=datetime.now(UTC) - timedelta(seconds=1)))
        s.commit()
    assert sync(accounts, worker_id="worker-b").imported == 1


def test_parallel_worker_is_blocked_by_lease(accounts):
    with get_sessionmaker()() as s:
        s.execute(update(Mailbox).where(Mailbox.id == mailbox_id(INFO))
                  .values(sync_locked_by="worker-a", sync_lease_until=datetime.now(UTC) + timedelta(minutes=5)))
        s.commit()
    assert sync(accounts, worker_id="worker-b").status == "skipped_locked"
    assert accounts[INFO].connects == 0


def test_inactive_mailbox_is_not_synced(accounts):
    with get_sessionmaker()() as s:
        s.execute(update(Mailbox).where(Mailbox.email_address == INFO).values(is_active=False))
        s.commit()
    assert sync(accounts).status == "skipped_inactive"


def test_missing_folder_does_not_block_other_folders(accounts):
    accounts[INFO].deliver(build_email("R"), ON_START + timedelta(hours=1))
    with get_sessionmaker()() as s:
        mb = s.get(Mailbox, mailbox_id(INFO))
        s.add(MailboxSyncState(company_id=mb.company_id, mailbox_id=mb.id, folder_name="Gibt-es-nicht"))
        s.commit()
    result = sync(accounts)
    assert result.status == "ok" and result.imported == 1
    with get_sessionmaker()() as s:
        missing = s.scalar(select(MailboxSyncState).where(MailboxSyncState.folder_name == "Gibt-es-nicht"))
    assert missing.missing_since is not None, "auf dem Server verschwundener Ordner wird markiert, nicht abgefragt"


def test_additional_folder_is_synced(accounts):
    accounts[INFO].deliver(build_email("in Ordner"), ON_START + timedelta(hours=1), folder="Rechnungen")
    with get_sessionmaker()() as s:
        mb = s.get(Mailbox, mailbox_id(INFO))
        s.add(MailboxSyncState(company_id=mb.company_id, mailbox_id=mb.id, folder_name="Rechnungen"))
        s.commit()
    assert sync(accounts).imported == 1
    assert messages()[0].folder_name == "Rechnungen"


# --- Worker-Planung -------------------------------------------------------------


def test_worker_syncs_due_mailboxes_and_respects_interval(accounts):
    s = syncer(accounts)
    settings = get_settings()
    first = run_once(s, get_sessionmaker(), settings)
    assert set(first) == {mailbox_id(INFO), mailbox_id(INVOICE)}
    assert run_once(s, get_sessionmaker(), settings) == {}, "Intervall noch nicht abgelaufen"
    with get_sessionmaker()() as db:
        db.execute(update(Mailbox).where(Mailbox.email_address == INFO).values(sync_requested_at=datetime.now(UTC)))
        db.commit()
    assert set(run_once(s, get_sessionmaker(), settings)) == {mailbox_id(INFO)}
