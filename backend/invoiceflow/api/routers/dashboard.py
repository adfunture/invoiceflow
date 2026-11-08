"""Kennzahlen für die Startseite des Dashboards."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from invoiceflow.api.deps import AuthContext, ensure_company, get_db, require_viewer
from invoiceflow.config import Settings, get_settings
from invoiceflow.db.enums import DocumentStatus, EmailMessageStatus, JobStatus, TransmissionStatus
from invoiceflow.db.models import (
    AlertState,
    DatevTransmission,
    Document,
    EmailMessage,
    Mailbox,
    ProcessingJob,
    WorkerHeartbeat,
)
from invoiceflow.recognition.model import INVOICE_LIKE

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


class Summary(BaseModel):
    recognized_today: int
    sent_total: int
    sent_today: int
    unconfirmed: int
    awaiting_send: int
    duplicates: int
    errors: int
    xml_queue: int
    manual_review_documents: int
    manual_review_messages: int
    manual_review_transmissions: int
    mailboxes_active: int
    mailboxes_with_errors: int
    stale_workers: list[str]
    test_mode_hint: str | None = None


@router.get("/summary", response_model=Summary)
def summary(
    company_id: int | None = Query(default=None),
    auth: AuthContext = Depends(require_viewer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Summary:
    if company_id is not None:
        ensure_company(auth, company_id)
    scope = {company_id} if company_id is not None else auth.company_ids

    def scoped(stmt, model):
        return stmt.where(model.company_id.in_(scope)) if scope is not None else stmt

    def count(model, *conditions) -> int:
        return db.scalar(scoped(select(func.count()).select_from(model).where(*conditions), model)) or 0

    tz = ZoneInfo(settings.import_timezone)
    today_start = datetime.combine(datetime.now(tz).date(), time.min, tzinfo=tz)
    live = DatevTransmission.test_mode.is_(False)
    sent_states = (TransmissionStatus.sent_unconfirmed, TransmissionStatus.confirmed)

    now = datetime.now(tz)
    stale = [w.worker_id for w in db.scalars(select(WorkerHeartbeat).where(
        WorkerHeartbeat.last_beat_at > now - timedelta(days=1), WorkerHeartbeat.last_beat_at < now - timedelta(minutes=3)))]

    return Summary(
        recognized_today=count(Document, Document.created_at >= today_start, Document.document_type.in_(INVOICE_LIKE)),
        sent_total=count(DatevTransmission, live, DatevTransmission.status.in_(sent_states)),
        sent_today=count(DatevTransmission, live, DatevTransmission.status.in_(sent_states), DatevTransmission.sent_at >= today_start),
        unconfirmed=count(DatevTransmission, live, DatevTransmission.status == TransmissionStatus.sent_unconfirmed),
        awaiting_send=count(DatevTransmission, live, DatevTransmission.status.in_((TransmissionStatus.pending, TransmissionStatus.ready))),
        duplicates=count(Document, Document.status == DocumentStatus.duplicate),
        errors=(count(Document, Document.status == DocumentStatus.error)
                + count(DatevTransmission, live, DatevTransmission.status == TransmissionStatus.failed)
                + count(ProcessingJob, ProcessingJob.status == JobStatus.dead)
                + count(EmailMessage, EmailMessage.status == EmailMessageStatus.error)),
        xml_queue=count(Document, Document.status == DocumentStatus.awaiting_datev_xml_integration),
        manual_review_documents=count(Document, Document.status == DocumentStatus.manual_review),
        manual_review_messages=count(EmailMessage, EmailMessage.status == EmailMessageStatus.manual_review),
        manual_review_transmissions=count(DatevTransmission, live, DatevTransmission.status == TransmissionStatus.manual_review),
        mailboxes_active=count(Mailbox, Mailbox.is_active.is_(True)),
        mailboxes_with_errors=count(Mailbox, Mailbox.is_active.is_(True), Mailbox.consecutive_failures > 0),
        stale_workers=stale,
        test_mode_hint=None if settings.datev_allow_real_send else "DATEV_ALLOW_REAL_SEND ist aus – es wird nichts an DATEV gesendet",
    )


class AlertOut(BaseModel):
    key: str
    severity: str
    message: str
    first_seen_at: datetime


@router.get("/alerts", response_model=list[AlertOut])
def alerts(auth: AuthContext = Depends(require_viewer), db: Session = Depends(get_db)) -> list[AlertOut]:
    """Aktive Befunde der Überwachung (Cron `invoiceflow ops monitor`)."""
    rows = db.scalars(select(AlertState).where(AlertState.active.is_(True))
                      .order_by(AlertState.severity, AlertState.first_seen_at))
    return [AlertOut(key=r.key, severity=r.severity, message=r.message, first_seen_at=r.first_seen_at) for r in rows]
