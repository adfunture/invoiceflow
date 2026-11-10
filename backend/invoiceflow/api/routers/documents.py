"""Dokumente: Übersicht mit Filtern, Detail, Original-Download, manuelle Prüfung, Neuerkennung."""
from __future__ import annotations

from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from invoiceflow.api.deps import AuthContext, ensure_company, get_db, require_reviewer, require_viewer
from invoiceflow.api.schemas import (
    BulkFailureOut,
    BulkIgnoreIn,
    BulkIgnoreOut,
    DocumentDetailOut,
    DocumentOut,
    DuplicateOut,
    InvoiceMetadataOut,
    ReviewIn,
)
from invoiceflow.db.enums import DocumentStatus, DocumentType
from invoiceflow.db.models import Document, EmailMessage, InvoiceDuplicate, InvoiceMetadata, Mailbox
from invoiceflow.services import reprocess as reprocess_svc
from invoiceflow.services import review as review_svc
from invoiceflow.services.audit import audit
from invoiceflow.storage import StorageError, get_store

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _out(doc: Document, meta: InvoiceMetadata | None, mailbox: str | None, received, cls=DocumentOut):
    out = cls.model_validate(doc)
    out.has_embedded_xml = doc.embedded_xml_storage_key is not None
    out.invoice = InvoiceMetadataOut.model_validate(meta) if meta else None
    out.mailbox_email, out.received_at = mailbox, received
    return out


@router.get("", response_model=list[DocumentOut])
def list_documents(
    company_id: int | None = Query(default=None),
    status: DocumentStatus | None = Query(default=None),
    hide_ignored: bool = Query(default=False, description="Ohne Status-Filter: ignorierte Dokumente ausblenden"),
    document_type: DocumentType | None = Query(default=None),
    supplier: str | None = Query(default=None, max_length=200),
    date_from: date | None = Query(default=None, description="Rechnungsdatum ab"),
    date_to: date | None = Query(default=None, description="Rechnungsdatum bis"),
    before_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    auth: AuthContext = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> list[DocumentOut]:
    stmt = (
        select(Document, InvoiceMetadata, Mailbox.email_address, EmailMessage.received_at)
        .join(EmailMessage, EmailMessage.id == Document.email_message_id)
        .join(Mailbox, Mailbox.id == EmailMessage.mailbox_id)
        .outerjoin(InvoiceMetadata, InvoiceMetadata.document_id == Document.id)
        .order_by(Document.id.desc()).limit(limit)
    )
    if company_id is not None:
        ensure_company(auth, company_id)
        stmt = stmt.where(Document.company_id == company_id)
    if auth.company_ids is not None:
        stmt = stmt.where(Document.company_id.in_(auth.company_ids))
    if status is not None:
        stmt = stmt.where(Document.status == status)
    elif hide_ignored:
        stmt = stmt.where(Document.status != DocumentStatus.ignored)
    if document_type is not None:
        stmt = stmt.where(Document.document_type == document_type)
    if supplier:
        escaped = supplier.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(InvoiceMetadata.supplier_name.ilike(f"%{escaped}%", escape="\\"))
    if date_from:
        stmt = stmt.where(InvoiceMetadata.invoice_date >= date_from)
    if date_to:
        stmt = stmt.where(InvoiceMetadata.invoice_date <= date_to)
    if before_id:
        stmt = stmt.where(Document.id < before_id)
    return [_out(d, m, mb, rec) for d, m, mb, rec in db.execute(stmt)]


def _load(db: Session, auth: AuthContext, doc_id: int) -> Document:
    doc = db.get(Document, doc_id)
    if doc is None or not auth.can_access(doc.company_id):
        raise HTTPException(404, "Nicht gefunden")
    return doc


@router.post("/bulk-ignore", response_model=BulkIgnoreOut)
def bulk_ignore(body: BulkIgnoreIn, auth: AuthContext = Depends(require_reviewer), db: Session = Depends(get_db)) -> BulkIgnoreOut:
    """Mehrere Dokumente auf „ignoriert“ setzen. Jedes einzeln geprüft und protokolliert;
    nicht änderbare (z. B. bereits freigegebene) werden übersprungen und gemeldet."""
    ignored: list[int] = []
    failed: list[BulkFailureOut] = []
    for doc_id in dict.fromkeys(body.document_ids):
        try:
            with db.begin_nested():
                review_svc.review_document(db, doc_id, auth.actor, decision="ignore", reason=body.reason,
                                           allowed_company_ids=auth.company_ids)
            ignored.append(doc_id)
        except review_svc.ReviewError as exc:
            failed.append(BulkFailureOut(document_id=doc_id, error=str(exc)))
    db.commit()
    return BulkIgnoreOut(ignored=ignored, failed=failed)


@router.get("/{doc_id}", response_model=DocumentDetailOut)
def get_document(doc_id: int, auth: AuthContext = Depends(require_viewer), db: Session = Depends(get_db)) -> DocumentDetailOut:
    doc = _load(db, auth, doc_id)
    meta = db.scalar(select(InvoiceMetadata).where(InvoiceMetadata.document_id == doc.id))
    msg = db.get(EmailMessage, doc.email_message_id)
    mailbox = db.get(Mailbox, msg.mailbox_id)
    out = _out(doc, meta, mailbox.email_address, msg.received_at, DocumentDetailOut)
    out.message_subject, out.message_from = msg.subject, msg.from_address
    dups = db.scalars(select(InvoiceDuplicate).where(
        (InvoiceDuplicate.document_id == doc.id) | (InvoiceDuplicate.duplicate_of_document_id == doc.id)))
    out.duplicates = [DuplicateOut.model_validate(d) for d in dups]
    return out


@router.get("/{doc_id}/original")
def download_original(doc_id: int, auth: AuthContext = Depends(require_viewer), db: Session = Depends(get_db)) -> Response:
    """Originaldatei unverändert (entschlüsselt, Hash geprüft). Jeder Abruf wird protokolliert."""
    doc = _load(db, auth, doc_id)
    try:
        data = get_store().get(doc.storage_key, doc.sha256)
    except StorageError as exc:
        raise HTTPException(500, "Original nicht lesbar – Integritätsprüfung fehlgeschlagen") from exc
    audit(db, auth.actor, "document.downloaded", company_id=doc.company_id, entity_type="document", entity_id=doc.id)
    db.commit()
    filename = doc.original_filename or f"dokument-{doc.id}"
    inline = doc.mime_type == "application/pdf"
    return Response(
        content=data,
        media_type=doc.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff",
            # Auch inline angezeigte PDFs dürfen keine Skripte der App ausführen.
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Document-SHA256": doc.sha256,
        },
    )


@router.post("/{doc_id}/review", response_model=DocumentDetailOut)
def review(doc_id: int, body: ReviewIn, auth: AuthContext = Depends(require_reviewer), db: Session = Depends(get_db)) -> DocumentDetailOut:
    try:
        review_svc.review_document(db, doc_id, auth.actor, decision=body.decision, reason=body.reason,
                                   corrections=body.corrections, duplicate_of=body.duplicate_of_document_id,
                                   allowed_company_ids=auth.company_ids)
    except review_svc.ReviewError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, str(exc)) from exc
    db.commit()
    return get_document(doc_id, auth, db)


@router.post("/{doc_id}/reprocess", response_model=DocumentDetailOut)
def reprocess(doc_id: int, auth: AuthContext = Depends(require_reviewer), db: Session = Depends(get_db)) -> DocumentDetailOut:
    """Dokument in Prüfung neu erkennen lassen (Regeln, OCR und ggf. KI im Processing-Worker)."""
    try:
        reprocess_svc.request_reprocess(db, doc_id, auth.actor, allowed_company_ids=auth.company_ids)
    except reprocess_svc.ReprocessError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, str(exc)) from exc
    db.commit()
    return get_document(doc_id, auth, db)
