"""Manuelle Eingriffe in den DATEV-Versand und Rückmeldungen.

Alle Funktionen laufen in der Transaktion des Aufrufers und schreiben ins
Audit-Log. ``record_feedback`` ist die Stelle, an der später automatische
DATEV-Rückmeldungen (z. B. Fehler-Mails von DATEV Upload Mail oder eine
API-Abfrage) andocken – dieselbe Logik wie die manuelle Bestätigung.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from invoiceflow.config import Settings
from invoiceflow.datev.message import MessageError, build_message
from invoiceflow.datev.sender import document_blockers
from invoiceflow.db.enums import AttemptOutcome, DatevChannel, DocumentStatus, TransmissionStatus
from invoiceflow.db.models import (
    DatevDestination,
    DatevTransmission,
    DatevTransmissionAttempt,
    Document,
    InvoiceMetadata,
)
from invoiceflow.services import app_settings
from invoiceflow.services.audit import Actor, audit
from invoiceflow.services.datev_gate import send_blockers
from invoiceflow.storage import DocumentStore, StorageError


class OperationError(ValueError):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(UTC)


def _load(db: Session, transmission_id: int, allowed: set[int] | None) -> DatevTransmission:
    t = db.scalar(select(DatevTransmission).where(DatevTransmission.id == transmission_id).with_for_update())
    if t is None or (allowed is not None and t.company_id not in allowed):
        raise OperationError("Nicht gefunden", 404)
    if t.test_mode:
        raise OperationError("Testlauf-Einträge können nicht bearbeitet werden", 409)
    return t


def request_send(db: Session, transmission_id: int, actor: Actor, settings: Settings, *,
                 mode: Literal["manual", "test_delivery"], allowed: set[int] | None = None) -> DatevTransmission:
    """Versand eines einzelnen Belegs anstoßen. Der Worker sendet ihn beim nächsten Durchlauf."""
    t = _load(db, transmission_id, allowed)
    if t.status not in (TransmissionStatus.pending, TransmissionStatus.failed):
        raise OperationError(f"Übertragung im Status {t.status.value} kann nicht gesendet werden", 409)
    dest = db.get(DatevDestination, t.destination_id)
    reasons = send_blockers(db, dest, settings, mode)[0] + document_blockers(db, db.get(Document, t.document_id))
    if reasons:
        raise OperationError("Versand nicht möglich: " + "; ".join(reasons))
    previous = t.status
    if previous == TransmissionStatus.failed:
        t.max_attempts = t.attempts + int(app_settings.get_setting(db, "datev.max_attempts", t.company_id))
    t.status = TransmissionStatus.ready
    t.send_mode = mode
    t.requested_by = actor.user_id
    t.requested_at = _now()
    t.next_attempt_at = _now()
    t.last_error = None
    audit(db, actor, f"datev.send_requested.{mode}", company_id=t.company_id, entity_type="datev_transmission",
          entity_id=t.id, details={"previous_status": previous.value, "to": dest.target_address})
    return t


def confirm(db: Session, transmission_id: int, actor: Actor, note: str, allowed: set[int] | None = None) -> DatevTransmission:
    t = _load(db, transmission_id, allowed)
    return record_feedback(db, t, actor, "confirmed", note, source="manual")


def record_feedback(db: Session, t: DatevTransmission, actor: Actor, outcome: Literal["confirmed", "rejected"],
                    note: str, *, source: str) -> DatevTransmission:
    """Rückmeldung zur Verarbeitung in DATEV (manuell oder später automatisch)."""
    if t.status != TransmissionStatus.sent_unconfirmed:
        raise OperationError(f"Nur gesendete, unbestätigte Übertragungen können bestätigt werden (Status {t.status.value})", 409)
    if outcome == "confirmed":
        t.status = TransmissionStatus.confirmed
        t.confirmed_at = _now()
        t.confirmed_by = actor.user_id
    else:
        # DATEV hat den Beleg abgelehnt: zur Klärung, kein automatischer Neuversand.
        t.status = TransmissionStatus.manual_review
        t.last_error = f"DATEV-Rückmeldung ({source}): {note}"[:2000]
    audit(db, actor, f"datev.feedback.{outcome}", company_id=t.company_id, entity_type="datev_transmission",
          entity_id=t.id, details={"note": note, "source": source})
    return t


def not_received(db: Session, transmission_id: int, actor: Actor, note: str,
                 allowed: set[int] | None = None) -> DatevTransmission:
    """Gesendet, aber nicht in DATEV angekommen (z. B. Bounce "User unknown"): Rückmeldung
    protokollieren und den Beleg wieder einplanen. Gesendet wird erst bei erfüllter Freigabe."""
    t = _load(db, transmission_id, allowed)
    record_feedback(db, t, actor, "rejected", note, source="manual")
    return resolve_unclear(db, transmission_id, actor, was_sent=False, reason=note, allowed=allowed)


def resolve_unclear(db: Session, transmission_id: int, actor: Actor, *, was_sent: bool, reason: str,
                    allowed: set[int] | None = None) -> DatevTransmission:
    """Klärt eine Übertragung mit unklarem Ergebnis. Erst danach ist ein erneuter Versand möglich."""
    t = _load(db, transmission_id, allowed)
    if t.status != TransmissionStatus.manual_review:
        raise OperationError("Nur Übertragungen in manueller Prüfung können geklärt werden", 409)
    doc = db.get(Document, t.document_id)
    if was_sent:
        t.status = TransmissionStatus.sent_unconfirmed
        t.sent_at = t.sent_at or t.sending_started_at or _now()
        doc.status = DocumentStatus.transmitted
    else:
        t.status = TransmissionStatus.pending
        t.send_mode = "automatic"
        t.max_attempts = t.attempts + int(app_settings.get_setting(db, "datev.max_attempts", t.company_id))
        if doc.status == DocumentStatus.transmitted:
            doc.status = DocumentStatus.ready_for_datev
    t.last_error = f"Geklärt ({'zugestellt' if was_sent else 'nicht zugestellt'}): {reason}"[:2000]
    audit(db, actor, "datev.unclear_resolved", company_id=t.company_id, entity_type="datev_transmission",
          entity_id=t.id, details={"was_sent": was_sent, "reason": reason})
    return t


def dry_run(db: Session, store: DocumentStore, document_id: int, actor: Actor,
            allowed: set[int] | None = None) -> tuple[DatevTransmission, dict]:
    """Testlauf: baut die vollständige DATEV-Mail, protokolliert sie und sendet **nichts**."""
    doc = db.get(Document, document_id)
    if doc is None or (allowed is not None and doc.company_id not in allowed):
        raise OperationError("Nicht gefunden", 404)
    dest = db.scalar(select(DatevDestination).where(
        DatevDestination.company_id == doc.company_id, DatevDestination.channel == DatevChannel.upload_mail))
    if dest is None or not dest.target_address:
        raise OperationError("Keine DATEV-Zieladresse für dieses Unternehmen")
    meta = db.scalar(select(InvoiceMetadata).where(InvoiceMetadata.document_id == doc.id))
    now = _now()
    t = DatevTransmission(
        company_id=doc.company_id, document_id=doc.id, destination_id=dest.id, channel=DatevChannel.upload_mail,
        destination_address=dest.target_address, file_sha256=doc.sha256, identity_hash=meta.identity_hash if meta else None,
        status=TransmissionStatus.confirmed, test_mode=True, send_mode="dry_run", attempts=1,
        requested_by=actor.user_id, requested_at=now, last_smtp_response="Testmodus – nicht gesendet",
    )
    db.add(t)
    db.flush()
    try:
        data = store.get(doc.storage_key, doc.sha256)
        message = build_message(doc=doc, meta=meta, data=data, sender=dest.sender_address or "absender@nicht-konfiguriert.invalid",
                                recipient=dest.target_address, transmission_id=t.id)
        error = None
    except (StorageError, MessageError) as exc:
        message, error = None, str(exc)
        t.status = TransmissionStatus.failed
        t.last_error = error
    db.add(DatevTransmissionAttempt(
        company_id=t.company_id, transmission_id=t.id, attempt_no=1, destination_address=dest.target_address,
        started_at=now, finished_at=now, outcome=AttemptOutcome.test_mode, test_mode=True,
        error_message=error, worker_id=actor.label,
    ))
    blockers = document_blockers(db, doc)
    preview = {
        "to": dest.target_address,
        "from": dest.sender_address,
        "subject": message["Subject"] if message else None,
        "attachment": message.get_payload()[1].get_filename() if message else None,
        "size_bytes": len(message.as_bytes()) if message else None,
        "document_blockers": blockers,
        "error": error,
    }
    audit(db, actor, "datev.dry_run", company_id=doc.company_id, entity_type="document", entity_id=doc.id,
          details={k: v for k, v in preview.items() if k != "size_bytes"})
    return t, preview
