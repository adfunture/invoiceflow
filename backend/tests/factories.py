"""Hilfsfunktionen zum Anlegen von Testdaten direkt in der DB."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from invoiceflow.db.enums import AttachmentSafety, SourceKind
from invoiceflow.db.models import Document, EmailAttachment, EmailMessage, Mailbox, MailboxSyncState


def mailbox(db: Session, email: str) -> Mailbox:
    return db.scalar(select(Mailbox).where(Mailbox.email_address == email))


def inbox(db: Session, mb: Mailbox) -> MailboxSyncState:
    return db.scalar(select(MailboxSyncState).where(MailboxSyncState.mailbox_id == mb.id, MailboxSyncState.folder_name == "INBOX"))


def message(db: Session, mb: Mailbox, uid: int, *, company_id: int | None = None, uidvalidity: int = 1) -> EmailMessage:
    msg = EmailMessage(
        company_id=company_id if company_id is not None else mb.company_id,
        mailbox_id=mb.id,
        sync_state_id=inbox(db, mb).id,
        folder_name="INBOX",
        uidvalidity=uidvalidity,
        uid=uid,
        received_at=datetime(2026, 11, 9, 10, 0, tzinfo=UTC),
        raw_sha256=hashlib.sha256(f"{mb.id}-{uid}".encode()).hexdigest(),
        raw_storage_key=f"raw/{mb.id}/{uid}.eml.enc",
        size_bytes=1234,
    )
    db.add(msg)
    db.flush()
    return msg


def document(db: Session, msg: EmailMessage, content: bytes, part_index: int = 1) -> Document:
    sha = hashlib.sha256(content).hexdigest()
    att = EmailAttachment(
        company_id=msg.company_id,
        email_message_id=msg.id,
        part_index=part_index,
        filename="rechnung.pdf",
        size_bytes=len(content),
        sha256=sha,
        storage_key=f"blobs/{sha}.enc",
        safety_status=AttachmentSafety.ok,
    )
    db.add(att)
    db.flush()
    doc = Document(
        company_id=msg.company_id,
        email_message_id=msg.id,
        attachment_id=att.id,
        sha256=sha,
        size_bytes=len(content),
        storage_key=att.storage_key,
        original_filename=att.filename,
        source_kind=SourceKind.pdf,
    )
    db.add(doc)
    db.flush()
    return doc
