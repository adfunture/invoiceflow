"""Importierte E-Mails (lesend) und Worker-Status."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from invoiceflow.api.deps import AuthContext, ensure_company, get_db, require_reviewer, require_viewer
from invoiceflow.api.schemas import AttachmentOut, EmailMessageDetailOut, EmailMessageOut, StrictInput, WorkerOut
from invoiceflow.db.enums import EmailMessageStatus
from invoiceflow.db.models import EmailAttachment, EmailMessage, WorkerHeartbeat
from invoiceflow.services.audit import audit

router = APIRouter(prefix="/api", tags=["messages"])
STALE_AFTER = timedelta(minutes=3)


@router.get("/email-messages", response_model=list[EmailMessageOut])
def list_messages(
    company_id: int | None = Query(default=None),
    mailbox_id: int | None = Query(default=None),
    status: EmailMessageStatus | None = Query(default=None),
    before_id: int | None = Query(default=None, description="Paginierung: nur ältere als diese ID"),
    limit: int = Query(default=50, ge=1, le=200),
    auth: AuthContext = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> list[EmailMessage]:
    stmt = select(EmailMessage).order_by(EmailMessage.id.desc()).limit(limit)
    if company_id is not None:
        ensure_company(auth, company_id)
        stmt = stmt.where(EmailMessage.company_id == company_id)
    if auth.company_ids is not None:
        stmt = stmt.where(EmailMessage.company_id.in_(auth.company_ids))
    if mailbox_id is not None:
        stmt = stmt.where(EmailMessage.mailbox_id == mailbox_id)
    if status is not None:
        stmt = stmt.where(EmailMessage.status == status)
    if before_id is not None:
        stmt = stmt.where(EmailMessage.id < before_id)
    return list(db.scalars(stmt))


@router.get("/email-messages/{message_id}", response_model=EmailMessageDetailOut)
def get_message(message_id: int, auth: AuthContext = Depends(require_viewer), db: Session = Depends(get_db)) -> EmailMessageDetailOut:
    msg = db.get(EmailMessage, message_id)
    if msg is None or not auth.can_access(msg.company_id):
        raise HTTPException(404, "Nicht gefunden")
    out = EmailMessageDetailOut.model_validate(msg)
    out.attachments = [
        AttachmentOut.model_validate(a)
        for a in db.scalars(select(EmailAttachment).where(EmailAttachment.email_message_id == msg.id).order_by(EmailAttachment.part_index))
    ]
    return out


@router.get("/workers", response_model=list[WorkerOut])
def list_workers(auth: AuthContext = Depends(require_viewer), db: Session = Depends(get_db)) -> list[WorkerOut]:
    now = datetime.now(UTC)
    rows = db.scalars(select(WorkerHeartbeat).where(WorkerHeartbeat.last_beat_at > now - timedelta(days=1))
                      .order_by(WorkerHeartbeat.worker_type, WorkerHeartbeat.last_beat_at.desc()))
    result = []
    for row in rows:
        out = WorkerOut.model_validate(row)
        out.stale = row.last_beat_at < now - STALE_AFTER
        result.append(out)
    return result


class MessageResolveIn(StrictInput):
    outcome: Literal["processed", "no_invoice"]
    reason: str = Field(min_length=3, max_length=2000)


class BulkMessageResolveIn(MessageResolveIn):
    message_ids: list[int] = Field(min_length=1, max_length=500)


class BulkMessageResolveOut(BaseModel):
    resolved: list[int]
    failed: list[int]


def _resolve(db: Session, auth: AuthContext, message_id: int, outcome: str, reason: str) -> None:
    msg = db.scalar(select(EmailMessage).where(EmailMessage.id == message_id).with_for_update())
    if msg is None or not auth.can_access(msg.company_id):
        raise HTTPException(404, "Nicht gefunden")
    if msg.status != EmailMessageStatus.manual_review:
        raise HTTPException(409, "Nur E-Mails in manueller Prüfung können abgeschlossen werden")
    previous_reason = msg.review_reason
    msg.status = EmailMessageStatus(outcome)
    msg.review_reason = f"Manuell geprüft: {reason}"[:4000]
    audit(db, auth.actor, "email_message.resolved", company_id=msg.company_id, entity_type="email_message",
          entity_id=msg.id, details={"outcome": outcome, "reason": reason, "previous_reason": previous_reason})


@router.post("/email-messages/bulk-resolve", response_model=BulkMessageResolveOut)
def bulk_resolve(body: BulkMessageResolveIn, auth: AuthContext = Depends(require_reviewer),
                 db: Session = Depends(get_db)) -> BulkMessageResolveOut:
    """Mehrere E-Mails auf einmal abschließen; nicht abschließbare werden übersprungen."""
    resolved: list[int] = []
    failed: list[int] = []
    for message_id in dict.fromkeys(body.message_ids):
        try:
            with db.begin_nested():
                _resolve(db, auth, message_id, body.outcome, body.reason)
            resolved.append(message_id)
        except HTTPException:
            failed.append(message_id)
    db.commit()
    return BulkMessageResolveOut(resolved=resolved, failed=failed)


@router.post("/email-messages/{message_id}/resolve", response_model=EmailMessageDetailOut)
def resolve_message(message_id: int, body: MessageResolveIn, auth: AuthContext = Depends(require_reviewer),
                    db: Session = Depends(get_db)) -> EmailMessageDetailOut:
    """Prüfvermerk für E-Mails (z. B. Rechnungslink manuell heruntergeladen und in DATEV erfasst)."""
    _resolve(db, auth, message_id, body.outcome, body.reason)
    db.commit()
    return get_message(message_id, auth, db)
