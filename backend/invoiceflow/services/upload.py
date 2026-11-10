"""Manueller Beleg-Upload (Papierbeleg, Foto, PDF aus WhatsApp oder einem Kundenportal).

Ein Upload läuft durch dieselbe Verarbeitung wie eine E-Mail: Je Unternehmen gibt
es ein internes Postfach „Manueller Upload“ (nie aktiv, kein IMAP). Jeder Upload
wird dort als Nachricht mit genau einem Anhang angelegt und als
``process_message``-Job eingeplant. Erkennung, KI-Fallback, Dublettenprüfung und
DATEV-Versand gelten damit unverändert.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from invoiceflow import jobs
from invoiceflow.db.enums import AttachmentSafety, EmailMessageStatus, EmailProvider, JobType
from invoiceflow.db.models import Company, EmailAttachment, EmailMessage, Mailbox, MailboxSyncState
from invoiceflow.mail.mime import classify_attachment, sanitize_filename
from invoiceflow.services import app_settings
from invoiceflow.services.audit import Actor, audit
from invoiceflow.storage import DocumentStore

# .invalid ist nach RFC 2606 reserviert – die Adresse kann nie ein echtes Postfach sein.
UPLOAD_DOMAIN = "upload.invoiceflow.invalid"
UPLOAD_FOLDER = "Manueller Upload"
UPLOAD_CREDENTIAL_REF = "INVOICEFLOW_SECRET_UPLOAD_UNUSED"
_EXTENSIONS = {"application/pdf": "pdf", "application/xml": "xml", "image/png": "png", "image/jpeg": "jpg",
               "image/tiff": "tif"}


class UploadError(ValueError):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def is_upload_mailbox(address: str | None) -> bool:
    return bool(address) and address.endswith("@" + UPLOAD_DOMAIN)


def _upload_state(db: Session, company: Company) -> tuple[Mailbox, MailboxSyncState]:
    address = f"upload-{company.slug}@{UPLOAD_DOMAIN}"
    db.execute(insert(Mailbox).values(
        company_id=company.id, email_address=address, display_name="Manueller Upload",
        provider=EmailProvider.other, credential_ref=UPLOAD_CREDENTIAL_REF, is_active=False,
    ).on_conflict_do_nothing(index_elements=[Mailbox.email_address]))
    mailbox = db.scalar(select(Mailbox).where(Mailbox.email_address == address))
    db.execute(insert(MailboxSyncState).values(
        company_id=company.id, mailbox_id=mailbox.id, folder_name=UPLOAD_FOLDER, is_enabled=False,
    ).on_conflict_do_nothing(constraint="uq_mailbox_sync_states_mailbox_folder"))
    # Zeile sperren: fortlaufende Upload-Nummern (uid) auch bei parallelen Uploads.
    state = db.scalar(select(MailboxSyncState).where(MailboxSyncState.mailbox_id == mailbox.id,
                                                     MailboxSyncState.folder_name == UPLOAD_FOLDER).with_for_update())
    return mailbox, state


def create_upload(db: Session, store: DocumentStore, *, company_id: int, actor: Actor, filename: str | None,
                  data: bytes, note: str | None = None) -> int:
    """Legt den Upload an und plant die Erkennung ein. → ID der (internen) Nachricht."""
    company = db.get(Company, company_id)
    if company is None:
        raise UploadError("Unternehmen nicht gefunden", 404)
    if not data:
        raise UploadError("Leere Datei")
    max_bytes = int(app_settings.get_setting(db, "attachments.max_size_mb", company_id)) * 1024 * 1024
    name = sanitize_filename(filename) or "upload"
    safety, detected, reason = classify_attachment(name, data, max_bytes)
    if safety != AttachmentSafety.ok:
        # Anders als bei E-Mails gibt es hier eine Person, die sofort reagieren kann: direkt ablehnen.
        raise UploadError(f"Datei nicht verwendbar: {reason}")

    obj = store.put(company.slug, "attachment", data, _EXTENSIONS.get(detected or "", "bin"))
    mailbox, state = _upload_state(db, company)
    uid = (db.scalar(select(func.max(EmailMessage.uid)).where(EmailMessage.sync_state_id == state.id)) or 0) + 1
    now = datetime.now(UTC)
    msg = EmailMessage(
        company_id=company.id, mailbox_id=mailbox.id, sync_state_id=state.id, folder_name=UPLOAD_FOLDER,
        uidvalidity=1, uid=uid, received_at=now, size_bytes=len(data), subject=(note or name)[:10_000],
        from_address=actor.label, to_addresses=[], attachment_count=1, status=EmailMessageStatus.fetched,
    )
    db.add(msg)
    db.flush()
    db.add(EmailAttachment(
        company_id=company.id, email_message_id=msg.id, part_index=1, filename=name,
        declared_content_type=detected, detected_content_type=detected, size_bytes=len(data),
        sha256=obj.sha256, storage_key=obj.key, safety_status=AttachmentSafety.ok,
    ))
    jobs.enqueue(db, company_id=company.id, job_type=JobType.process_message,
                 dedup_key=f"process_message:{msg.id}", payload={"email_message_id": msg.id}, priority=50)
    audit(db, actor, "document.uploaded", company_id=company.id, entity_type="email_message", entity_id=msg.id,
          details={"filename": name, "size_bytes": len(data), "sha256": obj.sha256, "type": detected})
    return msg.id
