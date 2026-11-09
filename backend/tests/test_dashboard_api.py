from __future__ import annotations

from invoiceflow.db.enums import UserRole

from .conftest import ApiClient, make_user
from .samples import invoice_lines, make_pdf, ubl_xml
from .test_imap_sync import INFO, INVOICE, accounts  # noqa: F401 – Fixture
from .test_pipeline import PDF, deliver, process


def test_summary_counts(admin, accounts):
    pdf = make_pdf(invoice_lines())
    deliver(accounts, [("r.pdf", pdf, PDF)], mailbox=INFO)
    deliver(accounts, [("r.pdf", pdf, PDF)], mailbox=INVOICE)
    deliver(accounts, [("x.xml", ubl_xml(), "application/xml")], mailbox=INFO)
    deliver(accounts, [("r2.pdf", make_pdf(invoice_lines("RE-9", recipient="SBC symbio GmbH & Co. KG")), PDF)], mailbox=INFO)
    process(accounts, INFO, INVOICE)
    s = admin.get("/api/dashboard/summary").json()
    assert s["recognized_today"] == 4
    assert s["duplicates"] == 1 and s["xml_queue"] == 1 and s["manual_review_documents"] == 1
    assert s["mailboxes_active"] == 2 and s["sent_total"] == 0
    assert s["test_mode_hint"]


def test_summary_scoped_to_company(admin, accounts):
    deliver(accounts, [("r.pdf", make_pdf(invoice_lines()), PDF)])
    process(accounts)
    make_user("sbc@sbc-symbio.de", UserRole.viewer, ("sbc",))
    sbc = ApiClient()
    sbc.login("sbc@sbc-symbio.de")
    assert sbc.get("/api/dashboard/summary").json()["recognized_today"] == 0
    assert admin.get("/api/dashboard/summary").json()["recognized_today"] == 1


def test_stale_workers_only_when_no_container_of_that_type_is_alive(admin):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from invoiceflow.db.models import WorkerHeartbeat
    from invoiceflow.db.session import get_sessionmaker
    from invoiceflow.workers.base import heartbeat

    old = datetime.now(UTC) - timedelta(minutes=10)
    with get_sessionmaker()() as s:
        heartbeat(s, "datev:alt:1", "datev", datetime.now(UTC), {})  # vor dem Neustart
        heartbeat(s, "datev:neu:1", "datev", datetime.now(UTC), {})  # neuer Container
        heartbeat(s, "imap:weg:1", "imap", datetime.now(UTC), {})    # wirklich ausgefallen
        s.execute(update(WorkerHeartbeat).where(WorkerHeartbeat.worker_id.in_(["datev:alt:1", "imap:weg:1"]))
                  .values(last_beat_at=old))
        s.commit()
    summary = admin.get("/api/dashboard/summary").json()
    assert summary["stale_workers"] == ["imap"]
