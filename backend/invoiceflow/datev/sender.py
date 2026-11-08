"""Versand von Belegen an DATEV Upload Mail.

Zustände einer Übertragung (``datev_transmissions.status``)
-----------------------------------------------------------
pending          angelegt; wartet auf Freigabe (Testmodus, Checkliste, Auto-Versand aus …)
ready            darf gesendet werden; der Worker nimmt sie auf
sending          SMTP-Versand läuft (vorher committet!)
sent_unconfirmed Mailserver hat angenommen; DATEV-Verarbeitung noch nicht bestätigt
confirmed        Eingang in DATEV bestätigt (manuell oder später per Rückmeldung)
failed           endgültig fehlgeschlagen; nur manuell erneut anstoßbar
manual_review    Ergebnis unklar (Abbruch während DATA / Worker-Absturz) – kein Zweitversand

Schutz vor Doppelversand
------------------------
* Je Dokument, Datei-Hash und Rechnungsidentität höchstens eine Live-Übertragung
  (partielle Unique-Indizes).
* Übernahme per ``FOR UPDATE SKIP LOCKED`` und Statuswechsel auf ``sending``
  in einer Transaktion *vor* dem SMTP-Aufruf.
* ``sending`` ohne Abschluss wird nie automatisch wiederholt, sondern geht in
  die manuelle Prüfung.
* Die Zieladresse stammt ausschließlich aus ``datev_destinations``.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from invoiceflow.config import Settings
from invoiceflow.datev.message import ATTACHMENT_TYPES, MessageError, build_message
from invoiceflow.datev.smtp import SmtpParams, SmtpResult, Transport
from invoiceflow.db.enums import AttemptOutcome, DatevChannel, DocumentStatus, SourceKind, TransmissionStatus
from invoiceflow.db.models import (
    DatevDestination,
    DatevTransmission,
    DatevTransmissionAttempt,
    Document,
    InvoiceMetadata,
)
from invoiceflow.logging_setup import redact_text
from invoiceflow.security.secrets import MissingSecretError, resolve_secret
from invoiceflow.services import app_settings
from invoiceflow.services.audit import Actor, audit
from invoiceflow.services.datev_gate import send_blockers
from invoiceflow.storage import DocumentStore, StorageError

log = logging.getLogger("invoiceflow.datev")
MAX_BACKOFF = timedelta(hours=1)
SENDABLE_MIME = set(ATTACHMENT_TYPES)


def utcnow() -> datetime:
    return datetime.now(UTC)


def document_blockers(db: Session, doc: Document) -> list[str]:
    """Belegbezogene Gründe gegen einen Versand per Upload Mail."""
    reasons: list[str] = []
    if doc.status not in (DocumentStatus.ready_for_datev, DocumentStatus.transmitted):
        reasons.append(f"Dokument hat Status {doc.status.value}")
    if doc.mime_type not in SENDABLE_MIME:
        reasons.append(f"Dateityp {doc.mime_type} nicht für Upload Mail vorgesehen")
    if doc.source_kind == SourceKind.zugferd_pdf and not app_settings.get_setting(
        db, "datev.upload_mail_zugferd_enabled", doc.company_id
    ):
        reasons.append("ZUGFeRD/Factur-X-Versand per Upload Mail noch nicht freigegeben (DATEV-Formatvorgabe prüfen)")
    max_bytes = int(app_settings.get_setting(db, "datev.max_mail_size_mb", doc.company_id)) * 1024 * 1024
    if doc.size_bytes * 4 / 3 + 10_000 > max_bytes:  # Base64-Aufschlag + Header
        reasons.append(f"Beleg zu groß für eine Upload-Mail ({doc.size_bytes // 1024} KB)")
    return reasons


@dataclass
class CycleResult:
    planned: int = 0
    promoted: int = 0
    recovered: int = 0
    sent: int = 0


class DatevSender:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        store: DocumentStore,
        settings: Settings,
        transport_factory: Callable[[], Transport],
        worker_id: str,
        *,
        stale_after: timedelta = timedelta(minutes=15),
    ) -> None:
        self.sessions = sessions
        self.store = store
        self.settings = settings
        self.transport_factory = transport_factory
        self.worker_id = worker_id
        self.actor = Actor.worker(worker_id)
        self.stale_after = stale_after

    def run_cycle(self, max_sends: int = 20) -> CycleResult:
        result = CycleResult(planned=self.plan(), recovered=self.recover(), promoted=self.promote())
        while result.sent < max_sends and self.send_next():
            result.sent += 1
        return result

    # --- 1. Übertragungen anlegen -------------------------------------------------

    def plan(self) -> int:
        created = 0
        with self.sessions() as db:
            live = exists().where(DatevTransmission.document_id == Document.id, DatevTransmission.test_mode.is_(False))
            docs = list(db.scalars(select(Document).where(Document.status == DocumentStatus.ready_for_datev, ~live)
                                   .order_by(Document.id).limit(500)))
            for doc in docs:
                dest = db.scalar(select(DatevDestination).where(
                    DatevDestination.company_id == doc.company_id, DatevDestination.channel == DatevChannel.upload_mail))
                if dest is None:
                    continue
                meta = db.scalar(select(InvoiceMetadata).where(InvoiceMetadata.document_id == doc.id))
                t = DatevTransmission(
                    company_id=doc.company_id, document_id=doc.id, destination_id=dest.id, channel=DatevChannel.upload_mail,
                    file_sha256=doc.sha256, identity_hash=meta.identity_hash if meta else None,
                    status=TransmissionStatus.pending, test_mode=False, send_mode="automatic",
                    max_attempts=int(app_settings.get_setting(db, "datev.max_attempts", doc.company_id)),
                )
                try:
                    with db.begin_nested():
                        db.add(t)
                    created += 1
                except IntegrityError:
                    # Gleiche Datei oder Rechnung hat bereits eine Live-Übertragung → nicht senden.
                    same = DatevTransmission.file_sha256 == doc.sha256
                    if meta is not None and meta.identity_hash:
                        same = same | (DatevTransmission.identity_hash == meta.identity_hash)
                    other = db.scalar(select(DatevTransmission.id).where(
                        DatevTransmission.company_id == doc.company_id, DatevTransmission.test_mode.is_(False), same).limit(1))
                    doc.status = DocumentStatus.manual_review
                    doc.review_reason = f"Gleiche Datei/Rechnung hat bereits die DATEV-Übertragung {other} – nicht erneut gesendet"
                    audit(db, self.actor, "datev.duplicate_blocked", company_id=doc.company_id,
                          entity_type="document", entity_id=doc.id, details={"transmission": other})
            db.commit()
        return created

    # --- 2. Freischalten (automatischer Versand) -------------------------------

    def promote(self) -> int:
        promoted = 0
        with self.sessions() as db:
            rows = list(db.scalars(select(DatevTransmission).where(
                DatevTransmission.status == TransmissionStatus.pending, DatevTransmission.test_mode.is_(False),
                DatevTransmission.send_mode == "automatic").order_by(DatevTransmission.id).limit(500)
                .with_for_update(skip_locked=True)))
            dest_cache: dict[int, list[str]] = {}
            for t in rows:
                if t.destination_id not in dest_cache:
                    dest_cache[t.destination_id] = send_blockers(db, db.get(DatevDestination, t.destination_id),
                                                                 self.settings, "automatic")[0]
                reasons = dest_cache[t.destination_id] + document_blockers(db, db.get(Document, t.document_id))
                if reasons:
                    t.last_error = "Wartet: " + "; ".join(reasons)
                else:
                    t.status = TransmissionStatus.ready
                    t.next_attempt_at = utcnow()
                    t.last_error = None
                    promoted += 1
            db.commit()
        return promoted

    # --- 3. Abgebrochene Versände sichern --------------------------------------

    def recover(self) -> int:
        cutoff = utcnow() - self.stale_after
        count = 0
        with self.sessions() as db:
            rows = list(db.scalars(select(DatevTransmission).where(
                DatevTransmission.status == TransmissionStatus.sending, DatevTransmission.sending_started_at < cutoff)
                .with_for_update(skip_locked=True)))
            for t in rows:
                t.status = TransmissionStatus.manual_review
                t.last_error = "Versand wurde unterbrochen (Worker-Neustart) – Zustellung unklar. Vor erneutem Versand prüfen."
                attempt = db.scalar(select(DatevTransmissionAttempt).where(
                    DatevTransmissionAttempt.transmission_id == t.id, DatevTransmissionAttempt.attempt_no == t.attempts))
                if attempt is not None and attempt.outcome is None:
                    attempt.outcome = AttemptOutcome.unknown
                    attempt.finished_at = utcnow()
                    attempt.error_message = "Kein Abschluss protokolliert"
                audit(db, self.actor, "datev.send_interrupted", company_id=t.company_id, entity_type="datev_transmission",
                      entity_id=t.id)
                count += 1
            db.commit()
        return count

    # --- 4. Senden ----------------------------------------------------------------

    def send_next(self) -> bool:
        """Sendet höchstens eine fällige Übertragung. False, wenn nichts fällig war."""
        now = utcnow()
        with self.sessions() as db:
            t = db.scalar(select(DatevTransmission).where(
                DatevTransmission.status == TransmissionStatus.ready, DatevTransmission.test_mode.is_(False),
                (DatevTransmission.next_attempt_at.is_(None)) | (DatevTransmission.next_attempt_at <= now))
                .order_by(DatevTransmission.next_attempt_at.nulls_first(), DatevTransmission.id)
                .limit(1).with_for_update(skip_locked=True))
            if t is None:
                db.commit()
                return False
            dest = db.get(DatevDestination, t.destination_id)
            doc = db.get(Document, t.document_id)
            meta = db.scalar(select(InvoiceMetadata).where(InvoiceMetadata.document_id == doc.id))
            reasons = send_blockers(db, dest, self.settings, t.send_mode)[0] + document_blockers(db, doc)
            if reasons:
                # Freigabe zwischenzeitlich entzogen (z. B. Testmodus wieder an) → zurück in die Warteschlange.
                t.status = TransmissionStatus.pending
                t.last_error = "Versand gestoppt: " + "; ".join(reasons)
                db.commit()
                return True
            t.status = TransmissionStatus.sending
            t.sending_started_at = now
            t.attempts += 1
            t.destination_address = dest.target_address
            attempt = DatevTransmissionAttempt(
                company_id=t.company_id, transmission_id=t.id, attempt_no=t.attempts, destination_address=dest.target_address,
                started_at=now, test_mode=False, worker_id=self.worker_id,
            )
            db.add(attempt)
            job = dict(
                transmission_id=t.id, attempt_no=t.attempts, recipient=dest.target_address, sender=dest.sender_address,
                credential_ref=dest.smtp_credential_ref, host=dest.smtp_host, port=dest.smtp_port,
                tls_mode=dest.smtp_tls_mode, username=dest.smtp_username, storage_key=doc.storage_key, sha=doc.sha256,
            )
            doc_snapshot, meta_snapshot = doc, meta
            db.commit()  # "sending" ist festgeschrieben, bevor irgendetwas das Haus verlässt

        try:
            data = self.store.get(job["storage_key"], job["sha"])
            message = build_message(doc=doc_snapshot, meta=meta_snapshot, data=data, sender=job["sender"],
                                    recipient=job["recipient"], transmission_id=job["transmission_id"])
            params = SmtpParams(host=job["host"], port=job["port"], tls_mode=job["tls_mode"], username=job["username"],
                                password=resolve_secret(job["credential_ref"]), timeout=self.settings.imap_timeout_seconds)
        except (StorageError, MessageError, MissingSecretError) as exc:
            # Nichts wurde gesendet; Ursache muss behoben werden → kein automatischer Wiederholversuch.
            self._finish(job, SmtpResult(AttemptOutcome.rejected, error=f"Vor dem Versand: {exc}"), None)
            return True

        try:
            result = self.transport_factory().send(message, params, job["sender"], job["recipient"])
        except Exception as exc:  # noqa: BLE001 – unbekannter Zustand, niemals blind wiederholen
            log.exception("smtp_transport_crashed", extra={"transmission_id": job["transmission_id"]})
            result = SmtpResult(AttemptOutcome.unknown, error=f"Transportfehler: {type(exc).__name__}")
        self._finish(job, result, message["Message-ID"])
        return True

    def _finish(self, job: dict, result: SmtpResult, message_id: str | None) -> None:
        now = utcnow()
        error = redact_text(result.error)[:2000] if result.error else None
        with self.sessions() as db:
            t = db.scalar(select(DatevTransmission).where(DatevTransmission.id == job["transmission_id"]).with_for_update())
            attempt = db.scalar(select(DatevTransmissionAttempt).where(
                DatevTransmissionAttempt.transmission_id == t.id, DatevTransmissionAttempt.attempt_no == job["attempt_no"]))
            attempt.finished_at = now
            attempt.outcome = result.outcome
            attempt.smtp_response_code = result.code
            attempt.smtp_response = result.response
            attempt.error_message = error
            t.last_smtp_response = result.response
            t.last_error = error
            details = {"outcome": result.outcome.value, "code": result.code, "attempt": job["attempt_no"],
                       "to": job["recipient"]}

            if result.outcome == AttemptOutcome.accepted:
                t.status = TransmissionStatus.sent_unconfirmed
                t.sent_at = now
                t.smtp_message_id = message_id
                doc = db.get(Document, t.document_id)
                doc.status = DocumentStatus.transmitted
                action = "datev.sent"
            elif result.outcome == AttemptOutcome.temporary_failure and t.attempts < t.max_attempts:
                t.status = TransmissionStatus.ready
                t.next_attempt_at = now + min(timedelta(seconds=60 * 2 ** t.attempts), MAX_BACKOFF)
                action = "datev.send_retry_scheduled"
            elif result.outcome == AttemptOutcome.unknown:
                t.status = TransmissionStatus.manual_review
                t.last_error = (error or "") + " – Zustellung unklar, vor erneutem Versand prüfen"
                action = "datev.send_unclear"
            else:
                t.status = TransmissionStatus.failed
                action = "datev.send_failed"
            audit(db, self.actor, action, company_id=t.company_id, entity_type="datev_transmission", entity_id=t.id,
                  details=details)
            db.commit()
        log.info("datev_send_finished", extra={"transmission_id": job["transmission_id"], "outcome": result.outcome.value,
                                                "code": result.code})
