"""Aufbewahrung und Löschung.

Regeln (siehe docs/SECURITY.md, Abschnitt Aufbewahrung):

* **Steuerlich relevante Belege** (alle Dokumente, deren E-Mails, Rechnungsdaten,
  Übertragungen, Audit-Log) werden **nie automatisch gelöscht**. Der Bericht
  zeigt, ab wann die Frist (``retention.original_documents_years``, mind. 8 Jahre
  ab Ende des Kalenderjahres) abläuft; gelöscht wird nur nach manueller Entscheidung.
* **E-Mails ohne Rechnungsbezug** (Status ``no_invoice``, keine Dokumente):
  Rohnachricht und Anhänge werden nach ``retention.non_invoice_emails_days``
  entfernt – nur wenn ``retention.auto_delete_enabled`` an ist. Metadaten
  (Absender, Betreff, Zeitpunkt) bleiben als Nachweis erhalten.
* Ein Speicherobjekt wird nur gelöscht, wenn kein anderer Datensatz darauf verweist
  (der Speicher ist inhaltsadressiert und kann geteilt sein).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from invoiceflow.db.enums import EmailMessageStatus
from invoiceflow.db.models import Document, EmailAttachment, EmailMessage
from invoiceflow.services import app_settings
from invoiceflow.services.audit import Actor, audit
from invoiceflow.storage import DocumentStore, StorageError


@dataclass
class RetentionReport:
    retention_years: int
    non_invoice_days: int
    auto_delete_enabled: bool
    documents_total: int
    documents_by_expiry_year: dict[int, int]
    purge_candidates: int


@dataclass
class PurgeResult:
    messages: int = 0
    deleted_objects: int = 0
    kept_shared_objects: int = 0
    errors: list[str] = field(default_factory=list)


def _candidates(db: Session, now: datetime, days: int):
    return (
        select(EmailMessage)
        .where(
            EmailMessage.status == EmailMessageStatus.no_invoice,
            EmailMessage.imported_at < now - timedelta(days=days),
            EmailMessage.raw_purged_at.is_(None),
            EmailMessage.raw_storage_key.is_not(None),
            ~exists().where(Document.email_message_id == EmailMessage.id),
        )
        .order_by(EmailMessage.id)
    )


def report(db: Session, now: datetime | None = None) -> RetentionReport:
    now = now or datetime.now(UTC)
    years = int(app_settings.get_setting(db, "retention.original_documents_years"))
    days = int(app_settings.get_setting(db, "retention.non_invoice_emails_days"))
    # Frist beginnt mit Ablauf des Kalenderjahres der Entstehung (§ 147 Abs. 4 AO):
    # Belege aus 2026 dürfen bei 10 Jahren frühestens nach dem 31.12.2036 gelöscht werden.
    year = func.extract("year", Document.created_at)
    by_year = {int(y) + years: n for y, n in db.execute(select(year, func.count()).group_by(year)).all()}
    return RetentionReport(
        retention_years=years,
        non_invoice_days=days,
        auto_delete_enabled=bool(app_settings.get_setting(db, "retention.auto_delete_enabled")),
        documents_total=sum(by_year.values()),
        documents_by_expiry_year=dict(sorted(by_year.items())),
        purge_candidates=db.scalar(select(func.count()).select_from(_candidates(db, now, days).subquery())) or 0,
    )


def _still_referenced(db: Session, key: str) -> bool:
    return bool(
        db.scalar(select(exists().where(Document.storage_key == key)))
        or db.scalar(select(exists().where(Document.embedded_xml_storage_key == key)))
        or db.scalar(select(exists().where(EmailAttachment.storage_key == key)))
        or db.scalar(select(exists().where(EmailMessage.raw_storage_key == key)))
    )


def purge_non_invoice_emails(db: Session, store: DocumentStore, actor: Actor, *, apply: bool,
                             now: datetime | None = None, limit: int = 500) -> PurgeResult:
    """Entfernt Rohdaten abgelaufener E-Mails ohne Rechnungsbezug. Ohne ``apply`` nur Zählung."""
    now = now or datetime.now(UTC)
    result = PurgeResult()
    if apply and not app_settings.get_setting(db, "retention.auto_delete_enabled"):
        raise PermissionError("retention.auto_delete_enabled ist aus – es wird nichts gelöscht")
    days = int(app_settings.get_setting(db, "retention.non_invoice_emails_days"))
    messages = list(db.scalars(_candidates(db, now, days).limit(limit)))
    result.messages = len(messages)
    if not apply:
        return result

    for msg in messages:
        keys = [msg.raw_storage_key]
        attachments = list(db.scalars(select(EmailAttachment).where(EmailAttachment.email_message_id == msg.id)))
        keys += [a.storage_key for a in attachments if a.storage_key]
        # Verweise zuerst entfernen, dann prüfen, ob noch jemand anderes das Objekt nutzt.
        msg.raw_storage_key = None
        msg.body_preview = None
        msg.raw_purged_at = now
        for a in attachments:
            a.storage_key = None
        db.flush()
        for key in keys:
            if _still_referenced(db, key):
                result.kept_shared_objects += 1
                continue
            try:
                store.delete(key)
                result.deleted_objects += 1
            except (StorageError, OSError) as exc:
                result.errors.append(f"{key}: {exc}")
    audit(db, actor, "retention.purged_non_invoice_emails", entity_type="email_message",
          details={"messages": result.messages, "deleted_objects": result.deleted_objects,
                   "kept_shared": result.kept_shared_objects, "older_than_days": days, "errors": result.errors[:20]})
    return result
