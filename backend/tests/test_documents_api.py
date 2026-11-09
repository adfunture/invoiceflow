from __future__ import annotations

from invoiceflow.db.enums import UserRole

from .conftest import ApiClient, company_id, make_user
from .samples import invoice_lines, make_pdf
from .test_imap_sync import INFO, accounts  # noqa: F401 – Fixture
from .test_pipeline import PDF, deliver, documents, process


def _setup(accounts, *attachments):
    deliver(accounts, list(attachments))
    process(accounts)
    return documents()


def test_list_filters_and_detail(admin, accounts):
    _setup(accounts, ("a.pdf", make_pdf(invoice_lines("RE-1")), PDF),
           ("b.pdf", make_pdf(invoice_lines("RE-2", recipient="SBC symbio GmbH & Co. KG")), PDF))
    all_docs = admin.get("/api/documents").json()
    assert len(all_docs) == 2
    ready = admin.get("/api/documents", params={"status": "ready_for_datev"}).json()
    assert [d["invoice"]["invoice_number"] for d in ready] == ["RE-1"]
    assert ready[0]["mailbox_email"] == INFO
    assert admin.get("/api/documents", params={"supplier": "müller"}).json()
    assert admin.get("/api/documents", params={"supplier": "%"}).json() == []
    assert admin.get("/api/documents", params={"date_from": "2026-12-01"}).json() == []
    detail = admin.get(f"/api/documents/{ready[0]['id']}").json()
    assert detail["message_subject"] == "Rechnung" and detail["invoice"]["gross_amount"] == "1190.00"


def test_original_download_is_unchanged_and_audited(admin, accounts):
    pdf = make_pdf(invoice_lines())
    (doc, _), = _setup(accounts, ("rechnung März.pdf", pdf, PDF))
    response = admin.get(f"/api/documents/{doc.id}/original")
    assert response.status_code == 200 and response.content == pdf
    assert response.headers["content-type"] == PDF
    assert "sandbox" in response.headers["content-security-policy"]
    assert "UTF-8''rechnung%20M%C3%A4rz.pdf" in response.headers["content-disposition"]
    logs = admin.get("/api/audit-logs", params={"entity_type": "document"}).json()
    assert logs[0]["action"] == "document.downloaded"


def test_review_approve_with_corrections(admin, accounts):
    lines = ["Lieferant GmbH", "Adfunture Outdoor GmbH", "Rechnung", "Rechnungsnummer: R-9", "Danke"]
    (doc, _), = _setup(accounts, ("r.pdf", make_pdf(lines), PDF))
    assert doc.status == "manual_review"
    url = f"/api/documents/{doc.id}/review"
    body = admin.post(url, json={"decision": "approve", "reason": "Betrag aus Papierbeleg ergänzt", "corrections": {
        "invoice_date": "2026-11-09", "gross_amount": "50.00", "currency": "eur"}}).json()
    assert body["status"] == "ready_for_datev"
    assert body["invoice"]["field_sources"]["gross_amount"] == "manual"
    log = admin.get("/api/audit-logs", params={"entity_type": "document"}).json()[0]
    assert log["action"] == "document.review.approve" and log["details"]["corrections"]["currency"]["new"] == "EUR"


def test_review_approve_without_data_is_one_click(admin, accounts):
    """Manuelle Freigabe ohne Rechnungsdaten: unklare Art wird Rechnung, Fehlendes wird nur protokolliert."""
    (doc, _), = _setup(accounts, ("spedition.pdf", make_pdf(["Spedition GmbH", "Seite 1", "Übertrag 219,82"]), PDF))
    assert doc.status == "manual_review"
    body = admin.post(f"/api/documents/{doc.id}/review", json={"decision": "approve", "reason": "Manuell geprüft"}).json()
    assert body["status"] == "ready_for_datev" and body["document_type"] == "invoice"
    log = admin.get("/api/audit-logs", params={"entity_type": "document"}).json()[0]
    assert "gross_amount" in log["details"]["missing_fields"] and log["details"]["document_type_set"]["new"] == "invoice"


def test_review_approve_blocks_identical_file_already_released(admin, accounts):
    pdf = make_pdf(["Spedition GmbH", "Seite 1"])
    _setup(accounts, ("a.pdf", pdf, PDF))
    deliver(accounts, [("b.pdf", pdf, PDF)], subject="nochmal")
    process(accounts)
    first, second = (d for d, _ in documents())
    assert admin.post(f"/api/documents/{first.id}/review", json={"decision": "approve", "reason": "ok ok"}).status_code == 200
    # Zweites Exemplar ist entweder schon als Dublette erkannt oder wird bei Freigabe blockiert.
    response = admin.post(f"/api/documents/{second.id}/review", json={"decision": "approve", "reason": "ok ok"})
    assert response.status_code == 409


def test_review_approve_blocked_if_same_invoice_already_released(admin, accounts):
    _setup(accounts, ("a.pdf", make_pdf(invoice_lines("RE-500")), PDF))
    deliver(accounts, [("b.pdf", make_pdf(invoice_lines("RE-500", title="Rechnungskorrektur", gross="1.071,00",
                                                          net="900,00", tax="171,00")), PDF)])
    process(accounts)
    corr = documents()[1][0]
    assert corr.status == "manual_review"
    # Prüfer bestätigt Korrektur als eigenständig → erlaubt (andere Identität).
    ok = admin.post(f"/api/documents/{corr.id}/review", json={"decision": "approve", "reason": "Korrektur ist gültig"})
    assert ok.status_code == 200 and ok.json()["status"] == "ready_for_datev"
    dup_decisions = {d["decision"] for d in ok.json()["duplicates"]}
    assert dup_decisions == {"not_duplicate"}
    # Wer die Korrektur auf die Originalwerte zurücksetzt, kollidiert mit dem freigegebenen Original.
    deliver(accounts, [("c.pdf", make_pdf(["Müller Outdoor Handel GmbH", "Rechnung", "Rechnungsnummer: RE-500"]), PDF)])
    process(accounts)
    third = documents()[2][0]
    clash = admin.post(f"/api/documents/{third.id}/review", json={"decision": "approve", "reason": "x" * 5, "corrections": {
        "invoice_date": "2026-11-09", "gross_amount": "1190.00", "currency": "EUR", "supplier_vat_id": "DE123456789"}})
    assert clash.status_code == 409


def test_review_ignore_and_mark_duplicate(admin, accounts):
    docs = _setup(accounts, ("a.pdf", make_pdf(invoice_lines("RE-1")), PDF),
                  ("b.pdf", make_pdf(invoice_lines("RE-2", recipient="SBC symbio GmbH & Co. KG")), PDF))
    held = docs[1][0]
    response = admin.post(f"/api/documents/{held.id}/review",
                          json={"decision": "ignore", "reason": "Gehört zu SBC – dort eingereicht"})
    assert response.json()["status"] == "ignored"
    response = admin.post(f"/api/documents/{held.id}/review",
                          json={"decision": "mark_duplicate", "reason": "doppelt", "duplicate_of_document_id": docs[0][0].id})
    assert response.json()["status"] == "duplicate"
    # Bereits freigegebene Dokumente können nicht erneut geprüft werden.
    assert admin.post(f"/api/documents/{docs[0][0].id}/review", json={"decision": "ignore", "reason": "abc"}).status_code == 409


def test_bulk_ignore_skips_released_and_foreign(admin, accounts):
    docs = _setup(accounts, ("a.pdf", make_pdf(invoice_lines("RE-1")), PDF),
                  ("b.pdf", make_pdf(["Lieferant GmbH", "Rechnung"]), PDF),
                  ("c.pdf", make_pdf(["Anderer GmbH", "Rechnung", "Rechnungsnummer: X-2"]), PDF))
    released, held1, held2 = (d for d, _ in docs)
    assert released.status == "ready_for_datev" and held1.status == held2.status == "manual_review"
    body = admin.post("/api/documents/bulk-ignore", json={
        "document_ids": [held1.id, held2.id, released.id, 999_999], "reason": "Keine Rechnung"}).json()
    assert body["ignored"] == [held1.id, held2.id]
    assert {f["document_id"] for f in body["failed"]} == {released.id, 999_999}
    assert admin.get("/api/documents", params={"status": "ignored"}).json().__len__() == 2
    logs = admin.get("/api/audit-logs", params={"entity_type": "document"}).json()
    assert [entry["action"] for entry in logs].count("document.review.ignore") == 2


def test_review_permissions_and_isolation(accounts):
    (doc, _), = _setup(accounts, ("r.pdf", make_pdf(["Lieferant GmbH", "Rechnung"]), PDF))
    make_user("viewer@adfunture.de", UserRole.viewer, ("adfunture",))
    viewer = ApiClient()
    viewer.login("viewer@adfunture.de")
    assert viewer.get(f"/api/documents/{doc.id}").status_code == 200
    assert viewer.post(f"/api/documents/{doc.id}/review", json={"decision": "ignore", "reason": "abc"}).status_code == 403
    make_user("pruefer@sbc-symbio.de", UserRole.reviewer, ("sbc",))
    sbc = ApiClient()
    sbc.login("pruefer@sbc-symbio.de")
    assert sbc.get(f"/api/documents/{doc.id}").status_code == 404
    assert sbc.get(f"/api/documents/{doc.id}/original").status_code == 404
    assert sbc.post(f"/api/documents/{doc.id}/review", json={"decision": "ignore", "reason": "abc"}).status_code == 404
    bulk = sbc.post("/api/documents/bulk-ignore", json={"document_ids": [doc.id], "reason": "abc"}).json()
    assert bulk["ignored"] == [] and bulk["failed"][0]["error"] == "Nicht gefunden"
    assert viewer.post("/api/documents/bulk-ignore", json={"document_ids": [doc.id], "reason": "abc"}).status_code == 403
    assert sbc.get("/api/documents", params={"company_id": company_id("adfunture")}).status_code == 404
