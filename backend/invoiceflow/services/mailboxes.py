"""Postfachverwaltung: Aktivierung, Domain-Bestätigung, Ordner."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from invoiceflow.db.models import Mailbox, MailboxSyncState
from invoiceflow.security.secrets import is_secret_configured
from invoiceflow.services.audit import Actor, audit

DEFAULT_FOLDER = "INBOX"


class MailboxError(ValueError):
    pass


def activation_blockers(mailbox: Mailbox) -> list[str]:
    """Was fehlt noch, bevor das Postfach abgerufen werden darf?"""
    blockers: list[str] = []
    if mailbox.domain_verified_at is None:
        blockers.append("Domain des Postfachs ist nicht bestätigt")
    if not mailbox.imap_host:
        blockers.append("IMAP-Host fehlt")
    if not mailbox.imap_username:
        blockers.append("IMAP-Benutzername fehlt")
    if not is_secret_configured(mailbox.credential_ref):
        blockers.append(f"Railway-Variable {mailbox.credential_ref} ist nicht gesetzt")
    return blockers


def verify_domain(db: Session, mailbox: Mailbox, confirmation: str, actor: Actor) -> None:
    domain = mailbox.email_address.split("@", 1)[1]
    if confirmation.strip().lower() != domain:
        raise MailboxError(f"Zur Bestätigung die Domain exakt eingeben ({domain})")
    mailbox.domain_verified_at = datetime.now(UTC)
    mailbox.domain_verified_by = actor.user_id
    audit(db, actor, "mailbox.domain_verified", company_id=mailbox.company_id, entity_type="mailbox",
          entity_id=mailbox.id, details={"domain": domain})


def set_active(db: Session, mailbox: Mailbox, active: bool, actor: Actor) -> None:
    if active:
        blockers = activation_blockers(mailbox)
        if blockers:
            raise MailboxError("; ".join(blockers))
    mailbox.is_active = active
    audit(db, actor, "mailbox.activated" if active else "mailbox.deactivated", company_id=mailbox.company_id,
          entity_type="mailbox", entity_id=mailbox.id)


def ensure_folder(db: Session, mailbox: Mailbox, folder_name: str, actor: Actor, *, enabled: bool = True) -> MailboxSyncState:
    folder_name = folder_name.strip()
    if not folder_name or len(folder_name) > 500 or any(ord(c) < 32 for c in folder_name):
        raise MailboxError("Ungültiger Ordnername")
    state = db.scalar(
        select(MailboxSyncState).where(MailboxSyncState.mailbox_id == mailbox.id, MailboxSyncState.folder_name == folder_name)
    )
    if state is None:
        state = MailboxSyncState(company_id=mailbox.company_id, mailbox_id=mailbox.id, folder_name=folder_name, is_enabled=enabled)
        db.add(state)
        db.flush()
        audit(db, actor, "mailbox.folder_added", company_id=mailbox.company_id, entity_type="mailbox",
              entity_id=mailbox.id, details={"folder": folder_name, "enabled": enabled})
    elif state.is_enabled != enabled:
        state.is_enabled = enabled
        audit(db, actor, "mailbox.folder_" + ("enabled" if enabled else "disabled"), company_id=mailbox.company_id,
              entity_type="mailbox", entity_id=mailbox.id, details={"folder": folder_name})
    return state
