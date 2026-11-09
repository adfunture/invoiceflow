from __future__ import annotations

from invoiceflow.config import get_settings
from invoiceflow.db.enums import AttemptOutcome, UserRole
from invoiceflow.datev.smtp import SmtpResult

from .conftest import ApiClient, make_user
from .samples import ubl_xml
from .test_datev_sender import TARGET, FakeTransport, configure_destination, ready_invoice, sender, transmissions
from .test_imap_sync import accounts  # noqa: F401 – Fixture
from .test_pipeline import deliver, documents, process


def _override_settings(client, settings):
    client.http.app.dependency_overrides[get_settings] = lambda: settings


def test_transmission_log_and_confirm(admin, accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    sender(FakeTransport()).run_cycle()
    t, = admin.get("/api/datev/transmissions").json()
    assert t["status"] == "sent_unconfirmed" and t["destination_address"] == TARGET
    assert t["supplier_name"] and t["invoice_number"]  # Absender der Rechnung in der Liste
    detail = admin.get(f"/api/datev/transmissions/{t['id']}").json()
    assert detail["attempts_log"][0]["outcome"] == "accepted"
    confirmed = admin.post(f"/api/datev/transmissions/{t['id']}/confirm", json={"note": "In DATEV sichtbar"})
    assert confirmed.json()["status"] == "confirmed"
    log = admin.get("/api/audit-logs", params={"entity_type": "datev_transmission"}).json()
    assert {e["action"] for e in log} >= {"datev.sent", "datev.feedback.confirmed"}


def test_bulk_confirm_only_sent_ones(admin, accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    sender(FakeTransport()).run_cycle()
    t, = admin.get("/api/datev/transmissions").json()
    body = admin.post("/api/datev/transmissions/bulk-confirm",
                      json={"transmission_ids": [t["id"], 999_999]}).json()
    assert body == {"confirmed": [t["id"]], "failed": [999_999]}
    assert admin.get(f"/api/datev/transmissions/{t['id']}").json()["status"] == "confirmed"
    # Bereits bestätigt → wird übersprungen, kein Fehler für die ganze Auswahl.
    again = admin.post("/api/datev/transmissions/bulk-confirm", json={"transmission_ids": [t["id"]]}).json()
    assert again == {"confirmed": [], "failed": [t["id"]]}


def test_bulk_send_requeues_failed_and_reports_blocked(admin, accounts, monkeypatch):
    from .test_datev_sender import real_send_settings

    configure_destination(monkeypatch)
    ready_invoice(accounts)
    sender(FakeTransport(SmtpResult(AttemptOutcome.rejected, 554, "5.6.0 CRLF"))).run_cycle()
    t, = admin.get("/api/datev/transmissions").json()
    assert t["status"] == "failed"
    # Ohne DATEV_ALLOW_REAL_SEND: nicht eingeplant, Grund wird gemeldet.
    blocked = admin.post("/api/datev/transmissions/bulk-send", json={"transmission_ids": [t["id"]]}).json()
    assert blocked["queued"] == [] and "DATEV_ALLOW_REAL_SEND" in blocked["failed"][0]["error"]
    _override_settings(admin, real_send_settings())
    body = admin.post("/api/datev/transmissions/bulk-send", json={"transmission_ids": [t["id"], 999_999]}).json()
    assert body["queued"] == [t["id"]] and [f["transmission_id"] for f in body["failed"]] == [999_999]
    assert admin.get(f"/api/datev/transmissions/{t['id']}").json()["status"] == "ready"


def test_not_received_requeues_bounced_transmission(admin, accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    sender(FakeTransport()).run_cycle()
    t, = admin.get("/api/datev/transmissions").json()
    body = admin.post(f"/api/datev/transmissions/{t['id']}/not-received", json={"note": "550 User unknown"}).json()
    assert body["status"] == "pending"
    log = admin.get("/api/audit-logs", params={"entity_type": "datev_transmission"}).json()
    assert {"datev.feedback.rejected", "datev.unclear_resolved"} <= {e["action"] for e in log}


def test_test_delivery_admin_only_and_gate(admin, accounts, monkeypatch):
    from .test_datev_sender import real_send_settings

    configure_destination(monkeypatch, steps=("address_verified", "sender_authorized", "assignment_tested",
                                               "duplicate_check_tested"), release=False)
    ready_invoice(accounts)
    sender(FakeTransport()).run_cycle()
    tid = transmissions()[0].id
    make_user("pruefer@adfunture.de", UserRole.reviewer, ("adfunture",))
    reviewer = ApiClient()
    reviewer.login("pruefer@adfunture.de")
    assert reviewer.post(f"/api/datev/transmissions/{tid}/send", json={"mode": "test_delivery"}).status_code == 403
    # Mit Standard-Settings (DATEV_ALLOW_REAL_SEND=false) lehnt die API ab.
    blocked = admin.post(f"/api/datev/transmissions/{tid}/send", json={"mode": "test_delivery"})
    assert blocked.status_code == 422 and "DATEV_ALLOW_REAL_SEND" in blocked.json()["detail"]
    _override_settings(admin, real_send_settings())
    ok = admin.post(f"/api/datev/transmissions/{tid}/send", json={"mode": "test_delivery"})
    assert ok.status_code == 200 and ok.json()["status"] == "ready"


def test_resolve_unclear_admin_only(admin, accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    sender(FakeTransport(SmtpResult(AttemptOutcome.unknown, error="timeout"))).run_cycle()
    tid = transmissions()[0].id
    make_user("pruefer@adfunture.de", UserRole.reviewer, ("adfunture",))
    reviewer = ApiClient()
    reviewer.login("pruefer@adfunture.de")
    body = {"was_sent": False, "reason": "In DATEV nicht vorhanden"}
    assert reviewer.post(f"/api/datev/transmissions/{tid}/resolve", json=body).status_code == 403
    assert admin.post(f"/api/datev/transmissions/{tid}/resolve", json=body).json()["status"] == "pending"


def test_dry_run_endpoint(admin, accounts, monkeypatch):
    configure_destination(monkeypatch, test_mode=True)
    ready_invoice(accounts)
    doc_id = documents()[0][0].id
    body = admin.post(f"/api/datev/documents/{doc_id}/dry-run").json()
    assert body["preview"]["to"] == TARGET and body["preview"]["size_bytes"] > 0
    assert admin.get("/api/datev/transmissions").json() == [], "Testläufe standardmäßig ausgeblendet"
    assert len(admin.get("/api/datev/transmissions", params={"include_test": True}).json()) == 1


def test_xml_queue_and_hand_over_not_configured(admin, accounts):
    deliver(accounts, [("xr.xml", ubl_xml(), "application/xml")])
    process(accounts)
    queue = admin.get("/api/datev/xml-queue").json()
    assert len(queue) == 1 and queue[0]["status"] == "awaiting_datev_xml_integration"
    response = admin.post(f"/api/datev/xml-queue/{queue[0]['id']}/hand-over")
    assert response.status_code == 503 and "Keine DATEV-Schnittstelle" in response.json()["detail"]
    assert admin.get("/api/datev/xml-queue").json()[0]["id"] == queue[0]["id"], "bleibt in der Warteschlange"


def test_transmissions_company_isolation(accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    sender(FakeTransport()).run_cycle()
    make_user("sbc@sbc-symbio.de", UserRole.reviewer, ("sbc",))
    sbc = ApiClient()
    sbc.login("sbc@sbc-symbio.de")
    tid = transmissions()[0].id
    assert sbc.get("/api/datev/transmissions").json() == []
    assert sbc.get(f"/api/datev/transmissions/{tid}").status_code == 404
    assert sbc.post(f"/api/datev/transmissions/{tid}/confirm", json={"note": "abc"}).status_code == 404
