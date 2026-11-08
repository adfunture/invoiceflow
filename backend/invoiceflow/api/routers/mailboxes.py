from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from invoiceflow.api.deps import AuthContext, ensure_company, get_db, require_admin, require_viewer
from invoiceflow.api.schemas import (
    ActiveIn,
    ConfirmIn,
    ConnectionTestOut,
    FolderIn,
    FolderOut,
    MailboxCreateIn,
    MailboxOut,
    MailboxUpdateIn,
)
from invoiceflow.config import Settings, get_settings
from invoiceflow.logging_setup import redact_text
from invoiceflow.mail.imap import ConnectionParams, MailServerError, MailServerFactory, default_factory
from invoiceflow.security.secrets import MissingSecretError, resolve_secret
from invoiceflow.db.models import Company, Mailbox, MailboxSyncState
from invoiceflow.security.secrets import is_secret_configured
from invoiceflow.services.mailboxes import DEFAULT_FOLDER
from invoiceflow.services import mailboxes as svc
from invoiceflow.services.audit import audit

router = APIRouter(prefix="/api/mailboxes", tags=["mailboxes"])


def _out(db: Session, mailbox: Mailbox) -> MailboxOut:
    folders = db.scalars(
        select(MailboxSyncState).where(MailboxSyncState.mailbox_id == mailbox.id).order_by(MailboxSyncState.folder_name)
    )
    out = MailboxOut.model_validate(mailbox)
    out.credential_configured = is_secret_configured(mailbox.credential_ref)
    out.activation_blockers = svc.activation_blockers(mailbox)
    out.folders = [FolderOut.model_validate(f) for f in folders]
    return out


def _load(db: Session, auth: AuthContext, mailbox_id: int, *, lock: bool = False) -> Mailbox:
    stmt = select(Mailbox).where(Mailbox.id == mailbox_id)
    if lock:
        stmt = stmt.with_for_update()
    mailbox = db.scalar(stmt)
    if mailbox is None or not auth.can_access(mailbox.company_id):
        raise HTTPException(404, "Nicht gefunden")
    return mailbox


@router.get("", response_model=list[MailboxOut])
def list_mailboxes(
    company_id: int | None = Query(default=None),
    auth: AuthContext = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> list[MailboxOut]:
    stmt = select(Mailbox).order_by(Mailbox.company_id, Mailbox.email_address)
    if company_id is not None:
        ensure_company(auth, company_id)
        stmt = stmt.where(Mailbox.company_id == company_id)
    if auth.company_ids is not None:
        stmt = stmt.where(Mailbox.company_id.in_(auth.company_ids))
    return [_out(db, m) for m in db.scalars(stmt)]


@router.get("/{mailbox_id}", response_model=MailboxOut)
def get_mailbox(mailbox_id: int, auth: AuthContext = Depends(require_viewer), db: Session = Depends(get_db)) -> MailboxOut:
    return _out(db, _load(db, auth, mailbox_id))


@router.post("", response_model=MailboxOut, status_code=201)
def create_mailbox(body: MailboxCreateIn, auth: AuthContext = Depends(require_admin), db: Session = Depends(get_db)) -> MailboxOut:
    if db.get(Company, body.company_id) is None:
        raise HTTPException(404, "Unternehmen nicht gefunden")
    mailbox = Mailbox(**body.model_dump(), is_active=False)
    mailbox.email_address = mailbox.email_address.lower()
    db.add(mailbox)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Postfach existiert bereits") from exc
    svc.ensure_folder(db, mailbox, svc.DEFAULT_FOLDER, auth.actor)
    audit(db, auth.actor, "mailbox.created", company_id=mailbox.company_id, entity_type="mailbox", entity_id=mailbox.id,
          details={"email": mailbox.email_address})
    db.commit()
    return _out(db, mailbox)


@router.patch("/{mailbox_id}", response_model=MailboxOut)
def update_mailbox(
    mailbox_id: int, body: MailboxUpdateIn, auth: AuthContext = Depends(require_admin), db: Session = Depends(get_db)
) -> MailboxOut:
    mailbox = _load(db, auth, mailbox_id, lock=True)
    changes = body.model_dump(exclude_unset=True)
    connection_fields = {"imap_host", "imap_port", "imap_tls_mode", "imap_username", "credential_ref"}
    for key, value in changes.items():
        setattr(mailbox, key, value)
    # Verbindungsänderung an einem aktiven Postfach: erst wieder aktivieren, wenn alles passt.
    if mailbox.is_active and connection_fields & changes.keys() and svc.activation_blockers(mailbox):
        mailbox.is_active = False
    audit(db, auth.actor, "mailbox.updated", company_id=mailbox.company_id, entity_type="mailbox", entity_id=mailbox.id,
          details=changes)
    db.commit()
    return _out(db, mailbox)


@router.post("/{mailbox_id}/verify-domain", response_model=MailboxOut)
def verify_domain(
    mailbox_id: int, body: ConfirmIn, auth: AuthContext = Depends(require_admin), db: Session = Depends(get_db)
) -> MailboxOut:
    mailbox = _load(db, auth, mailbox_id, lock=True)
    try:
        svc.verify_domain(db, mailbox, body.confirmation, auth.actor)
    except svc.MailboxError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return _out(db, mailbox)


@router.post("/{mailbox_id}/active", response_model=MailboxOut)
def set_active(
    mailbox_id: int, body: ActiveIn, auth: AuthContext = Depends(require_admin), db: Session = Depends(get_db)
) -> MailboxOut:
    mailbox = _load(db, auth, mailbox_id, lock=True)
    try:
        svc.set_active(db, mailbox, body.active, auth.actor)
    except svc.MailboxError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return _out(db, mailbox)


@router.post("/{mailbox_id}/folders", response_model=MailboxOut)
def upsert_folder(
    mailbox_id: int, body: FolderIn, auth: AuthContext = Depends(require_admin), db: Session = Depends(get_db)
) -> MailboxOut:
    mailbox = _load(db, auth, mailbox_id, lock=True)
    try:
        svc.ensure_folder(db, mailbox, body.folder_name, auth.actor, enabled=body.enabled)
    except svc.MailboxError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return _out(db, mailbox)


def get_mail_server_factory() -> MailServerFactory:
    """In Tests überschreibbar (dependency_overrides)."""
    return default_factory


@router.post("/{mailbox_id}/test-connection", response_model=ConnectionTestOut)
def test_connection(
    mailbox_id: int,
    auth: AuthContext = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    factory: MailServerFactory = Depends(get_mail_server_factory),
) -> ConnectionTestOut:
    """Anmelden, Ordner auflisten, INBOX nur lesend öffnen. Ändert nichts im Postfach."""
    mailbox = _load(db, auth, mailbox_id)
    db.commit()  # keine Transaktion während des Netzwerkzugriffs offen halten
    if not mailbox.imap_host or not mailbox.imap_username:
        return ConnectionTestOut(ok=False, error="IMAP-Host oder Benutzername fehlt")
    out = ConnectionTestOut(ok=False)
    try:
        server = factory(ConnectionParams(
            host=mailbox.imap_host, port=mailbox.imap_port, tls_mode=mailbox.imap_tls_mode,
            username=mailbox.imap_username, password=resolve_secret(mailbox.credential_ref),
            timeout=min(settings.imap_timeout_seconds, 20),
        ))
        try:
            out.folders = [f.name for f in server.list_folders() if f.selectable]
            info = server.select_folder(DEFAULT_FOLDER)
            out.inbox_uidvalidity, out.inbox_messages = info.uidvalidity, info.exists
            out.ok = True
        finally:
            server.close()
    except (MailServerError, MissingSecretError) as exc:
        out.error = redact_text(str(exc))[:500]
    audit(db, auth.actor, "mailbox.connection_tested", company_id=mailbox.company_id, entity_type="mailbox",
          entity_id=mailbox.id, details={"ok": out.ok, "error": out.error})
    db.commit()
    return out


@router.post("/{mailbox_id}/sync-now", response_model=MailboxOut)
def sync_now(mailbox_id: int, auth: AuthContext = Depends(require_admin), db: Session = Depends(get_db)) -> MailboxOut:
    """Markiert das Postfach; der IMAP-Worker ruft es beim nächsten Durchlauf (≤ 15 s) ab."""
    mailbox = _load(db, auth, mailbox_id, lock=True)
    if not mailbox.is_active:
        raise HTTPException(422, "Postfach ist nicht aktiv")
    mailbox.sync_requested_at = datetime.now(UTC)
    audit(db, auth.actor, "mailbox.sync_requested", company_id=mailbox.company_id, entity_type="mailbox", entity_id=mailbox.id)
    db.commit()
    return _out(db, mailbox)
