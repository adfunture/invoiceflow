"""Erneute Erkennung eines Dokuments (z. B. nachdem der KI-Fallback eingerichtet wurde).

Nur Dokumente, die maschinell in der Prüfung gelandet sind, dürfen neu erkannt
werden. Freigegebene, übertragene, ignorierte oder als Dublette entschiedene
Belege bleiben unangetastet – eine Neuerkennung könnte sonst eine bewusste
Entscheidung still überschreiben.

Die Erkennung selbst läuft im Processing-Worker (dort liegen KI-Schlüssel und
OCR): Das Dokument wird auf ``pending`` gesetzt und ein ``process_message``-Job
eingeplant. Die Pipeline verarbeitet dann genau die nicht-terminalen Dokumente
der Nachricht neu.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from invoiceflow import jobs
from invoiceflow.db.enums import DocumentStatus, JobType
from invoiceflow.db.models import Document, InvoiceDuplicate
from invoiceflow.services.audit import Actor, audit

REPROCESSABLE = {DocumentStatus.manual_review, DocumentStatus.error}


class ReprocessError(ValueError):
    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def request_reprocess(db: Session, doc_id: int, actor: Actor, *, allowed_company_ids: set[int] | None = None) -> Document:
    doc = db.scalar(select(Document).where(Document.id == doc_id).with_for_update())
    if doc is None or (allowed_company_ids is not None and doc.company_id not in allowed_company_ids):
        raise ReprocessError("Nicht gefunden", 404)
    if doc.status not in REPROCESSABLE:
        raise ReprocessError(f"Dokument im Status {doc.status.value} kann nicht neu erkannt werden")

    previous = doc.status
    doc.status = DocumentStatus.pending
    doc.review_reason = None
    doc.error_message = None
    # Maschinell angelegte Dublettenhinweise entstehen bei der Neuerkennung neu;
    # von Personen entschiedene Einträge bleiben erhalten.
    db.execute(delete(InvoiceDuplicate).where(InvoiceDuplicate.document_id == doc.id,
                                              InvoiceDuplicate.decided_by.is_(None)))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    jobs.enqueue(db, company_id=doc.company_id, job_type=JobType.process_message,
                 dedup_key=f"reprocess_document:{doc.id}:{stamp}",
                 payload={"email_message_id": doc.email_message_id, "reprocess_document_id": doc.id},
                 priority=50)
    audit(db, actor, "document.reprocess_requested", company_id=doc.company_id, entity_type="document",
          entity_id=doc.id, details={"previous_status": previous.value})
    return doc
