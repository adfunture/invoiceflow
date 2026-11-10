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
    assert [d["id"] for d in admin.get("/api/documents", params={"hide_ignored": "true"}).json()] == [released.id]
    assert len(admin.get("/api/documents").json()) == 3
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


def test_reprocess_reruns_recognition_with_ai(admin, accounts):
    """Beleg landete ohne KI in der Prüfung; nach „Neu erkennen“ wertet der Worker ihn mit KI aus."""
    from invoiceflow.db.session import get_sessionmaker
    from invoiceflow.recognition import ai as ai_mod
    from invoiceflow.workers.processing_worker import run_once

    from .test_pipeline import UNCLEAR, FakeAi, _enable_ai, pdf_att, processor

    (doc, _), = _setup(accounts, pdf_att(UNCLEAR))
    assert doc.status == "manual_review"
    _enable_ai()
    body = admin.post(f"/api/documents/{doc.id}/reprocess").json()
    assert body["status"] == "pending" and body["review_reason"] is None
    ai = FakeAi(ai_mod.AiInvoiceFields(document_type="invoice", confidence=0.95, invoice_number="2026/55",
                                       invoice_date="2026-11-09", gross_amount="119.00", currency="EUR",
                                       supplier_name="Lieferant Beispiel GmbH"))
    assert run_once(processor(ai=ai), get_sessionmaker(), "test-processing") == 1
    assert ai.calls == 1
    detail = admin.get(f"/api/documents/{doc.id}").json()
    assert detail["status"] == "ready_for_datev", detail["review_reason"]
    assert detail["invoice"]["invoice_number"] == "2026/55"
    log = admin.get("/api/audit-logs", params={"entity_type": "document"}).json()[0]
    assert log["action"] == "document.reprocess_requested" and log["details"]["previous_status"] == "manual_review"


def test_reprocess_refuses_decided_documents_and_foreign_companies(admin, accounts):
    (doc, _), = _setup(accounts, ("r.pdf", make_pdf(["Lieferant GmbH", "Rechnung"]), PDF))
    assert admin.post(f"/api/documents/{doc.id}/review", json={"decision": "ignore", "reason": "kein Beleg"}).status_code == 200
    response = admin.post(f"/api/documents/{doc.id}/reprocess")
    assert response.status_code == 409
    (ready, _), = [(d, m) for d, m in _setup(accounts, ("a.pdf", make_pdf(invoice_lines("RE-77")), PDF))
                   if d.status == "ready_for_datev"]
    assert admin.post(f"/api/documents/{ready.id}/reprocess").status_code == 409
    make_user("viewer2@adfunture.de", UserRole.viewer, ("adfunture",))
    viewer = ApiClient()
    viewer.login("viewer2@adfunture.de")
    assert viewer.post(f"/api/documents/{doc.id}/reprocess").status_code == 403
    make_user("pruefer2@sbc-symbio.de", UserRole.reviewer, ("sbc",))
    sbc = ApiClient()
    sbc.login("pruefer2@sbc-symbio.de")
    assert sbc.post(f"/api/documents/{doc.id}/reprocess").status_code == 404


def test_bulk_reprocess_reports_not_allowed(admin, accounts):
    from .test_pipeline import UNCLEAR, pdf_att

    docs = _setup(accounts, pdf_att(UNCLEAR, "a.pdf"), ("b.pdf", make_pdf(invoice_lines("RE-88")), PDF))
    review = next(d for d, _ in docs if d.status == "manual_review")
    ready = next(d for d, _ in docs if d.status == "ready_for_datev")
    body = admin.post("/api/documents/bulk-reprocess", json={"document_ids": [review.id, ready.id, 999_999]}).json()
    assert body["queued"] == [review.id]
    assert {f["document_id"] for f in body["failed"]} == {ready.id, 999_999}
    assert admin.get(f"/api/documents/{review.id}").json()["status"] == "pending"


def test_approve_can_remember_sender_as_invoice_sender(admin, accounts):
    """Gelernte Absenderregel: der nächste unklare Beleg desselben Absenders wird als Rechnung eingestuft."""
    from .test_pipeline import deliver, pdf_att, process

    receipt = ["Lieferant Beispiel GmbH", "Adfunture Outdoor GmbH", "Receipt", "Rechnungsnummer: R-1",
               "Datum: 09.11.2026", "Gesamtbetrag 119,00 EUR", "USt-IdNr.: DE123456789"]
    (doc, _), = _setup(accounts, pdf_att(receipt))
    assert doc.status == "manual_review" and doc.document_type == "unclear"
    body = admin.post(f"/api/documents/{doc.id}/review",
                      json={"decision": "approve", "reason": "Manuell geprüft", "remember_sender": True}).json()
    assert body["status"] == "ready_for_datev"
    settings = {s["key"]: s["value"] for s in admin.get("/api/settings").json()}
    assert settings["recognition.invoice_senders"] == ["buchhaltung@lieferant.example"]

    deliver(accounts, [pdf_att([*receipt[:3], "Rechnungsnummer: R-2", *receipt[4:]], "r2.pdf")], subject="Receipt 2")
    process(accounts)
    second = [d for d, _ in documents() if d.id != doc.id][0]
    assert second.document_type == "invoice"
    assert second.status == "ready_for_datev", second.review_reason
