from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from invoiceflow.api.routers.mailboxes import get_mail_server_factory
from invoiceflow.db.enums import UserRole
from invoiceflow.db.models import Mailbox
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
        heartbeat(s, "imap:b:2", "imap", datetime.now(UTC), {})
        from invoiceflow.db.models import WorkerHeartbeat
        s.execute(update(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == "imap:b:2")
                  .values(last_beat_at=datetime.now(UTC) - timedelta(minutes=10)))
        s.commit()
    workers = {w["worker_id"]: w["stale"] for w in admin.get("/api/workers").json()}
    assert workers == {"imap:a:1": False, "imap:b:2": True}


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
