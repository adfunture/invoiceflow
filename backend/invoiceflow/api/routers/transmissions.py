"""DATEV-Übertragungen: Versandprotokoll, manueller Versand, Bestätigung, Klärung, Testlauf, XML-Warteschlange."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from invoiceflow.api.deps import AuthContext, ensure_company, get_db, require_admin, require_reviewer, require_viewer
from invoiceflow.api.schemas import (
    AttemptOut,
    DocumentOut,
    DryRunOut,
    NoteIn,
    ResolveIn,
    SendRequestIn,
    TransmissionDetailOut,
    TransmissionOut,
)
from invoiceflow.config import Settings, get_settings
from invoiceflow.datev import operations as ops
from invoiceflow.datev.xml_channel import XmlChannelUnavailable, get_xml_channel
from invoiceflow.db.enums import DocumentStatus, TransmissionStatus, UserRole
from invoiceflow.db.models import DatevTransmission, DatevTransmissionAttempt, Document
from invoiceflow.services.audit import audit
from invoiceflow.storage import StorageError, get_store

router = APIRouter(prefix="/api/datev", tags=["datev-transmissions"])


def _detail(db: Session, t: DatevTransmission) -> TransmissionDetailOut:
    out = TransmissionDetailOut.model_validate(t)
    out.attempts_log = [AttemptOut.model_validate(a) for a in db.scalars(
        select(DatevTransmissionAttempt).where(DatevTransmissionAttempt.transmission_id == t.id)
        .order_by(DatevTransmissionAttempt.attempt_no))]
    return out


def _run(db: Session, fn, *args, **kwargs):
    try:
        result = fn(*args, **kwargs)
    except ops.OperationError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, str(exc)) from exc
    db.commit()
    return result


@router.get("/transmissions", response_model=list[TransmissionOut])
def list_transmissions(
    company_id: int | None = Query(default=None),
    status: TransmissionStatus | None = Query(default=None),
    include_test: bool = Query(default=False),
    document_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    auth: AuthContext = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> list[DatevTransmission]:
    stmt = select(DatevTransmission).order_by(DatevTransmission.id.desc()).limit(limit)
    if company_id is not None:
        ensure_company(auth, company_id)
        stmt = stmt.where(DatevTransmission.company_id == company_id)
    if auth.company_ids is not None:
        stmt = stmt.where(DatevTransmission.company_id.in_(auth.company_ids))
    if status is not None:
        stmt = stmt.where(DatevTransmission.status == status)
    if not include_test:
        stmt = stmt.where(DatevTransmission.test_mode.is_(False))
    if document_id is not None:
        stmt = stmt.where(DatevTransmission.document_id == document_id)
    return list(db.scalars(stmt))


@router.get("/transmissions/{transmission_id}", response_model=TransmissionDetailOut)
def get_transmission(transmission_id: int, auth: AuthContext = Depends(require_viewer), db: Session = Depends(get_db)):
    t = db.get(DatevTransmission, transmission_id)
    if t is None or not auth.can_access(t.company_id):
        raise HTTPException(404, "Nicht gefunden")
    return _detail(db, t)


@router.post("/transmissions/{transmission_id}/send", response_model=TransmissionDetailOut)
def request_send(transmission_id: int, body: SendRequestIn, auth: AuthContext = Depends(require_reviewer),
                 db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    """Einzelnen Beleg senden. ``test_delivery`` (vor der Produktivfreigabe) nur für Admins."""
    if body.mode == "test_delivery" and auth.user.role != UserRole.admin:
        raise HTTPException(403, "Testlieferungen an DATEV nur durch Admins")
    t = _run(db, ops.request_send, db, transmission_id, auth.actor, settings, mode=body.mode, allowed=auth.company_ids)
    return _detail(db, t)


@router.post("/transmissions/{transmission_id}/confirm", response_model=TransmissionDetailOut)
def confirm(transmission_id: int, body: NoteIn, auth: AuthContext = Depends(require_reviewer), db: Session = Depends(get_db)):
    """Manuelle Bestätigung: Beleg ist in DATEV Unternehmen online angekommen."""
    t = _run(db, ops.confirm, db, transmission_id, auth.actor, body.note, allowed=auth.company_ids)
    return _detail(db, t)


@router.post("/transmissions/{transmission_id}/resolve", response_model=TransmissionDetailOut)
def resolve(transmission_id: int, body: ResolveIn, auth: AuthContext = Depends(require_admin), db: Session = Depends(get_db)):
    """Unklaren Versand klären (in DATEV nachsehen!). Erst danach ist ein erneuter Versand möglich."""
    t = _run(db, ops.resolve_unclear, db, transmission_id, auth.actor, was_sent=body.was_sent, reason=body.reason,
             allowed=auth.company_ids)
    return _detail(db, t)


@router.post("/documents/{document_id}/dry-run", response_model=DryRunOut)
def dry_run(document_id: int, auth: AuthContext = Depends(require_reviewer), db: Session = Depends(get_db)) -> DryRunOut:
    """Baut die DATEV-Mail vollständig auf und protokolliert sie – es wird nichts gesendet."""
    t, preview = _run(db, ops.dry_run, db, get_store(), document_id, auth.actor, allowed=auth.company_ids)
    return DryRunOut(transmission_id=t.id, preview=preview)


@router.get("/xml-queue", response_model=list[DocumentOut])
def xml_queue(company_id: int | None = Query(default=None), auth: AuthContext = Depends(require_viewer),
              db: Session = Depends(get_db)) -> list[DocumentOut]:
    stmt = select(Document).where(Document.status == DocumentStatus.awaiting_datev_xml_integration).order_by(Document.id)
    if company_id is not None:
        ensure_company(auth, company_id)
        stmt = stmt.where(Document.company_id == company_id)
    if auth.company_ids is not None:
        stmt = stmt.where(Document.company_id.in_(auth.company_ids))
    return [DocumentOut.model_validate(d) for d in db.scalars(stmt)]


@router.post("/xml-queue/{document_id}/hand-over")
def xml_hand_over(document_id: int, auth: AuthContext = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    doc = db.get(Document, document_id)
    if doc is None or not auth.can_access(doc.company_id):
        raise HTTPException(404, "Nicht gefunden")
    if doc.status != DocumentStatus.awaiting_datev_xml_integration:
        raise HTTPException(409, "Dokument ist nicht in der XML-Warteschlange")
    channel = get_xml_channel()
    try:
        data = get_store().get(doc.storage_key, doc.sha256)
        reference = channel.submit(doc, data)
    except XmlChannelUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(500, "Original nicht lesbar") from exc
    doc.status = DocumentStatus.transmitted
    audit(db, auth.actor, "datev.xml_handed_over", company_id=doc.company_id, entity_type="document", entity_id=doc.id,
          details={"channel": channel.name, "reference": reference})
    db.commit()
    return {"status": "transmitted", "reference": reference}
