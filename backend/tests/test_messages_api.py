from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from invoiceflow.api.routers.mailboxes import get_mail_server_factory
from invoiceflow.db.enums import UserRole
from invoiceflow.db.models import Mailbox, WorkerHeartbeat
from invoiceflow.db.session import get_sessionmaker
from invoiceflow.workers.base import heartbeat

from .conftest import ApiClient, make_user
from .fake_imap import FakeAccount, build_email, factory_for
from .test_imap_sync import INFO, ON_START, sync


def _box_id(email: str) -> int:
    with get_sessionmaker()() as s:
        return s.scalar(select(Mailbox.id).where(Mailbox.email_address == email))


def _override(client: ApiClient, accounts) -> None:
    client.http.app.dependency_overrides[get_mail_server_factory] = lambda: factory_for(accounts)


def test_connection_test_success(admin, monkeypatch):
    monkeypatch.setenv("INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO", "richtig")
    account = FakeAccount()
    account.deliver(build_email(), ON_START)
    _override(admin, {INFO: account})
    body = admin.post(f"/api/mailboxes/{_box_id(INFO)}/test-connection").json()
    assert body["ok"] is True and body["folders"] == ["INBOX"] and body["inbox_messages"] == 1
    assert account.raw_fetches == [], "Testverbindung lädt keine Nachrichten"


def test_connection_test_wrong_password(admin, monkeypatch):
    monkeypatch.setenv("INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO", "falsch-xyz")
    _override(admin, {INFO: FakeAccount()})
    body = admin.post(f"/api/mailboxes/{_box_id(INFO)}/test-connection").json()
    assert body["ok"] is False and "authentication failed" in body["error"]
    assert "falsch-xyz" not in body["error"]


def test_connection_test_requires_admin(monkeypatch):
    make_user("viewer@adfunture.de", UserRole.viewer, ("adfunture",))
    client = ApiClient()
    client.login("viewer@adfunture.de")
    assert client.post(f"/api/mailboxes/{_box_id(INFO)}/test-connection").status_code == 403


def test_sync_now_only_for_active_mailbox(admin):
    url = f"/api/mailboxes/{_box_id(INFO)}/sync-now"
    assert admin.post(url).status_code == 422
    with get_sessionmaker()() as s:
        s.execute(update(Mailbox).where(Mailbox.email_address == INFO).values(is_active=True))
        s.commit()
    body = admin.post(url).json()
    assert body["sync_requested_at"] is not None


def test_message_list_is_company_scoped(admin, monkeypatch):
    monkeypatch.setenv("INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO", "richtig")
    with get_sessionmaker()() as s:
        s.execute(update(Mailbox).where(Mailbox.email_address == INFO).values(is_active=True))
        s.commit()
    account = FakeAccount()
    account.deliver(build_email("Rechnung 77", attachments=[("r.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")]),
                    ON_START + timedelta(hours=1))
    sync({INFO: account})

    listed = admin.get("/api/email-messages").json()
    assert [m["subject"] for m in listed] == ["Rechnung 77"]
    detail = admin.get(f"/api/email-messages/{listed[0]['id']}").json()
    assert detail["attachments"][0]["filename"] == "r.pdf"
    assert "storage_key" not in detail["attachments"][0]

    make_user("sbc@sbc-symbio.de", UserRole.reviewer, ("sbc",))
    sbc = ApiClient()
    sbc.login("sbc@sbc-symbio.de")
    assert sbc.get("/api/email-messages").json() == []
    assert sbc.get(f"/api/email-messages/{listed[0]['id']}").status_code == 404


def test_workers_endpoint_marks_stale(admin):
    with get_sessionmaker()() as s:
        heartbeat(s, "imap:a:1", "imap", datetime.now(UTC), {})
        heartbeat(s, "datev:b:2", "datev", datetime.now(UTC), {})
        s.execute(update(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == "datev:b:2")
                  .values(last_beat_at=datetime.now(UTC) - timedelta(minutes=10)))
        s.commit()
    workers = {w["worker_id"]: w["stale"] for w in admin.get("/api/workers").json()}
    assert workers == {"imap:a:1": False, "datev:b:2": True}


def test_resolve_message_review(admin, monkeypatch):
    from .test_pipeline import deliver, messages, process

    monkeypatch.setenv("INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO", "richtig")
    with get_sessionmaker()() as s:
        s.execute(update(Mailbox).where(Mailbox.email_address == INFO).values(is_active=True))
        s.commit()
    accounts = {INFO: FakeAccount(), "invoice@adfunture.de": FakeAccount()}
    deliver(accounts, [], body="Ihre Rechnung: https://portal.example.com/invoice/9")
    process(accounts)
    msg_id = messages()[0].id
    url = f"/api/email-messages/{msg_id}/resolve"
    body = admin.post(url, json={"outcome": "processed", "reason": "PDF aus Portal geladen und in DATEV erfasst"}).json()
    assert body["status"] == "processed"
    assert admin.post(url, json={"outcome": "processed", "reason": "nochmal"}).status_code == 409


def test_bulk_resolve_messages(admin, monkeypatch):
    from .test_pipeline import deliver, messages, process

    monkeypatch.setenv("INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO", "richtig")
    with get_sessionmaker()() as s:
        s.execute(update(Mailbox).where(Mailbox.email_address == INFO).values(is_active=True))
        s.commit()
    accounts = {INFO: FakeAccount(), "invoice@adfunture.de": FakeAccount()}
    deliver(accounts, [], body="Rechnung: https://portal.example.com/invoice/1")
    deliver(accounts, [], body="Rechnung: https://portal.example.com/invoice/2")
    process(accounts)
    ids = [m.id for m in messages()]
    body = admin.post("/api/email-messages/bulk-resolve",
                      json={"message_ids": [*ids, 999_999], "outcome": "no_invoice", "reason": "Manuell geprüft"}).json()
    assert sorted(body["resolved"]) == sorted(ids) and body["failed"] == [999_999]
    assert {m.status for m in messages()} == {"no_invoice"}


def test_block_senders_resolves_open_mails_and_stops_import(admin, monkeypatch):
    from invoiceflow.services import app_settings

    from .test_pipeline import deliver, messages, process

    monkeypatch.setenv("INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO", "richtig")
    with get_sessionmaker()() as s:
        s.execute(update(Mailbox).where(Mailbox.email_address == INFO).values(is_active=True))
        s.commit()
    accounts = {INFO: FakeAccount(), "invoice@adfunture.de": FakeAccount()}
    body_text = "Zahlungsbeleg: https://www.paypal.com/unifiedtransactions/details/payment/1"
    deliver(accounts, [], subject="Zahlungseingang 1", body=body_text, sender="service@paypal.de")
    deliver(accounts, [], subject="Zahlungseingang 2", body=body_text, sender="Service@PayPal.de")
    deliver(accounts, [], body="Rechnung: https://portal.example.com/invoice/3", sender="buchhaltung@lieferant.example")
    process(accounts)
    first, second, other = messages()

    make_user("pruefer3@adfunture.de", UserRole.reviewer, ("adfunture",))
    reviewer = ApiClient()
    reviewer.login("pruefer3@adfunture.de")
    assert reviewer.post("/api/email-messages/block-senders", json={"message_ids": [first.id]}).status_code == 403

    body = admin.post("/api/email-messages/block-senders", json={"message_ids": [first.id, 999_999]}).json()
    assert body["blocked"] == ["service@paypal.de"] and body["failed"] == [999_999]
    assert sorted(body["resolved"]) == sorted([first.id, second.id])
    statuses = {m.id: m.status for m in messages()}
    assert statuses[first.id] == statuses[second.id] == "no_invoice" and statuses[other.id] == "manual_review"
    with get_sessionmaker()() as s:
        assert app_settings.get_setting(s, "mail.blocked_senders") == ["service@paypal.de"]
    log = admin.get("/api/audit-logs", params={"entity_type": "setting"}).json()[0]
    assert log["action"] == "mail.senders_blocked" and log["details"]["added"] == ["service@paypal.de"]

    deliver(accounts, [], subject="Zahlungseingang 3", body=body_text, sender="service@paypal.de")
    process(accounts)
    assert len(messages()) == 3


def test_attachment_only_mode_keeps_mails_with_documents_open(admin, monkeypatch):
    from invoiceflow.services import app_settings

    from .test_pipeline import PDF, deliver, messages, process

    monkeypatch.setenv("INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO", "richtig")
    with get_sessionmaker()() as s:
        s.execute(update(Mailbox).where(Mailbox.email_address == INFO).values(is_active=True))
        s.commit()
    accounts = {INFO: FakeAccount(), "invoice@adfunture.de": FakeAccount()}
    deliver(accounts, [], subject="Zahlungseingang", body="Zahlungsbeleg: https://www.paypal.com/x/payment/1",
            sender="service@paypal.de")
    deliver(accounts, [("kaputt.pdf", b"%PDF-1.4 kaputt", PDF)], subject="Beleg", sender="service@paypal.de")
    process(accounts)
    note, with_doc = messages()
    body = admin.post("/api/email-messages/block-senders",
                      json={"message_ids": [note.id], "mode": "attachment_only"}).json()
    assert body["blocked"] == ["service@paypal.de"] and note.id in body["resolved"] and with_doc.id not in body["resolved"]
    with get_sessionmaker()() as s:
        assert app_settings.get_setting(s, "mail.attachment_only_senders") == ["service@paypal.de"]
        assert app_settings.get_setting(s, "mail.blocked_senders") == []


def test_workers_list_hides_replaced_workers(admin):
    from invoiceflow.workers.base import remove_heartbeat

    now = datetime.now(UTC)
    with get_sessionmaker()() as s:
        heartbeat(s, "datev:old:1", "datev", now - timedelta(hours=3), {})
        s.execute(update(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == "datev:old:1")
                  .values(last_beat_at=now - timedelta(hours=2)))
        s.commit()
        heartbeat(s, "datev:new:1", "datev", now, {})
        heartbeat(s, "imap:dead:1", "imap", now - timedelta(hours=3), {})
        s.execute(update(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == "imap:dead:1")
                  .values(last_beat_at=now - timedelta(hours=1)))
        s.commit()
    workers = {w["worker_id"]: w for w in admin.get("/api/workers").json()}
    assert "datev:new:1" in workers and "datev:old:1" not in workers
    assert workers["imap:dead:1"]["stale"] is True  # einziger imap-Worker und stumm: bleibt sichtbar
    remove_heartbeat(get_sessionmaker(), "datev:new:1")
    assert "datev:new:1" not in {w["worker_id"] for w in admin.get("/api/workers").json()}
