"""Abruf eines Postfachs: Backfill ab Startdatum, danach inkrementell per UID.

Garantien
---------
* Nachrichten vor ``import_start_at`` (08.11.2026 00:00 Europe/Berlin) werden
  nie importiert. Maßgeblich ist das IMAP-INTERNALDATE (Zustellzeit beim
  Server), nicht der fälschbare Date-Header.
* Jede Nachricht wird in genau einer Transaktion gespeichert, zusammen mit
  dem Fortschritt ``last_seen_uid``. Stirbt der Worker, setzt der nächste
  Lauf nach der letzten vollständig gespeicherten Nachricht fort.
* ``(postfach, ordner, UIDVALIDITY, UID)`` ist unique; zusätzlich wird eine
  bereits gespeicherte identische Rohnachricht im selben Postfach erkannt
  (z. B. nach UIDVALIDITY-Wechsel). Mehrfacher Abruf erzeugt keine Duplikate.
* Ein Lease auf dem Postfach verhindert parallelen Abruf durch zwei Worker.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from invoiceflow import jobs
from invoiceflow.config import Settings
from invoiceflow.db.enums import AttachmentSafety, EmailMessageStatus, JobType
from invoiceflow.db.models import Company, EmailAttachment, EmailMessage, Mailbox, MailboxSyncState
from invoiceflow.logging_setup import redact_text
from invoiceflow.mail.imap import ConnectionParams, MailServer, MailServerError, MailServerFactory, MessageMeta
from invoiceflow.mail.mime import ParsedMessage, parse_message
from invoiceflow.security.secrets import MissingSecretError, resolve_secret
from invoiceflow.services import app_settings
from invoiceflow.services.audit import Actor, audit
from invoiceflow.storage import DocumentStore, StorageError

log = logging.getLogger("invoiceflow.sync")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _clean(value: str | None, limit: int) -> str | None:
    """PostgreSQL-Text darf kein NUL enthalten; Header sind nicht vertrauenswürdig."""
    if value is None:
        return None
    return value.replace("\x00", "")[:limit] or None


@dataclass
class FolderResult:
    folder: str
    imported: int = 0
    already_known: int = 0
    before_start: int = 0
    uidvalidity_changed: bool = False


@dataclass
class SyncResult:
    mailbox_id: int
    status: str  # ok | skipped_locked | skipped_inactive | failed
    folders: list[FolderResult] = field(default_factory=list)
    error: str | None = None

    @property
    def imported(self) -> int:
        return sum(f.imported for f in self.folders)


class MailboxSyncer:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        store: DocumentStore,
        settings: Settings,
        server_factory: MailServerFactory,
        worker_id: str,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.sessions = session_factory
        self.store = store
        self.settings = settings
        self.server_factory = server_factory
        self.worker_id = worker_id
        self.sleep = sleep
        self.actor = Actor.worker(worker_id)
        self._lease_renewed_at = 0.0

    # --- Lease ---------------------------------------------------------------

    def _acquire(self, db: Session, mailbox_id: int) -> bool:
        now = utcnow()
        row = db.execute(
            update(Mailbox)
            .where(
                Mailbox.id == mailbox_id,
                Mailbox.is_active.is_(True),
                (Mailbox.sync_lease_until.is_(None)) | (Mailbox.sync_lease_until < now) | (Mailbox.sync_locked_by == self.worker_id),
            )
            .values(
                sync_locked_by=self.worker_id,
                sync_lease_until=now + timedelta(seconds=self.settings.mailbox_sync_lease_seconds),
                last_sync_started_at=now,
                sync_requested_at=None,
            )
            .returning(Mailbox.id)
        ).first()
        db.commit()
        self._lease_renewed_at = time.monotonic()
        return row is not None

    def _renew(self, mailbox_id: int) -> None:
        if time.monotonic() - self._lease_renewed_at < self.settings.mailbox_sync_lease_seconds / 3:
            return
        with self.sessions() as db:
            db.execute(
                update(Mailbox)
                .where(Mailbox.id == mailbox_id, Mailbox.sync_locked_by == self.worker_id)
                .values(sync_lease_until=utcnow() + timedelta(seconds=self.settings.mailbox_sync_lease_seconds))
            )
            db.commit()
        self._lease_renewed_at = time.monotonic()

    def _release(self, mailbox_id: int) -> None:
        with self.sessions() as db:
            db.execute(
                update(Mailbox)
                .where(Mailbox.id == mailbox_id, Mailbox.sync_locked_by == self.worker_id)
                .values(sync_locked_by=None, sync_lease_until=None)
            )
            db.commit()

    # --- Verbindung ----------------------------------------------------------

    def _connect(self, mailbox: Mailbox) -> MailServer:
        if not mailbox.imap_host or not mailbox.imap_username:
            raise MailServerError("IMAP-Host oder Benutzername fehlt")
        params = ConnectionParams(
            host=mailbox.imap_host,
            port=mailbox.imap_port,
            tls_mode=mailbox.imap_tls_mode,
            username=mailbox.imap_username,
            password=resolve_secret(mailbox.credential_ref),
            timeout=self.settings.imap_timeout_seconds,
        )
        attempts = max(1, self.settings.imap_connect_retries)
        for attempt in range(1, attempts + 1):
            try:
                return self.server_factory(params)
            except MailServerError as exc:
                log.warning("imap_connect_failed", extra={"mailbox_id": mailbox.id, "attempt": attempt, "error": str(exc)})
                if attempt == attempts:
                    raise
                self.sleep(min(2 ** attempt, 30))
        raise AssertionError("unreachable")

    # --- Ablauf --------------------------------------------------------------

    def sync_mailbox(self, mailbox_id: int) -> SyncResult:
        with self.sessions() as db:
            mailbox = db.get(Mailbox, mailbox_id)
            if mailbox is None or not mailbox.is_active:
                return SyncResult(mailbox_id, "skipped_inactive")
            if not self._acquire(db, mailbox_id):
                return SyncResult(mailbox_id, "skipped_locked")
            company_slug = db.scalar(select(Company.slug).where(Company.id == mailbox.company_id))
            folder_names = list(
                db.scalars(
                    select(MailboxSyncState.folder_name)
                    .where(MailboxSyncState.mailbox_id == mailbox_id, MailboxSyncState.is_enabled.is_(True))
                    .order_by(MailboxSyncState.id)
                )
            )
            max_attachment = int(app_settings.get_setting(db, "attachments.max_size_mb", mailbox.company_id)) * 1024 * 1024
            max_message = int(app_settings.get_setting(db, "imap.max_message_size_mb", mailbox.company_id)) * 1024 * 1024
            db.expunge(mailbox)

        result = SyncResult(mailbox_id, "ok")
        try:
            server = self._connect(mailbox)
            try:
                for name in folder_names:
                    try:
                        result.folders.append(
                            self._sync_folder(server, mailbox, company_slug, name, max_attachment, max_message)
                        )
                    except MailServerError as exc:
                        # Ein defekter Ordner hält die übrigen nicht auf; Fortschritt bleibt erhalten.
                        self._folder_error(mailbox.id, name, str(exc))
                        result.status = "failed"
                        result.error = redact_text(f"{name}: {exc}")[:2000]
            finally:
                server.close()
        except (MailServerError, MissingSecretError, StorageError) as exc:
            result.status = "failed"
            result.error = redact_text(str(exc))[:2000]
        except Exception as exc:  # noqa: BLE001 – Worker muss weiterlaufen; Fehler wird sichtbar gemacht
            log.exception("mailbox_sync_crashed", extra={"mailbox_id": mailbox_id})
            result.status = "failed"
            result.error = redact_text(f"Interner Fehler: {type(exc).__name__}: {exc}")[:2000]
        finally:
            self._finish(mailbox_id, result)
            self._release(mailbox_id)
        log.info("mailbox_synced", extra={"mailbox_id": mailbox_id, "status": result.status,
                                          "imported": result.imported, "error": result.error})
        return result

    def _folder_error(self, mailbox_id: int, folder: str, error: str) -> None:
        with self.sessions() as db:
            db.execute(
                update(MailboxSyncState)
                .where(MailboxSyncState.mailbox_id == mailbox_id, MailboxSyncState.folder_name == folder)
                .values(last_error=redact_text(error)[:2000])
            )
            db.commit()

    def _finish(self, mailbox_id: int, result: SyncResult) -> None:
        now = utcnow()
        with self.sessions() as db:
            mailbox = db.get(Mailbox, mailbox_id)
            if result.status == "ok":
                mailbox.last_successful_sync_at = now
                mailbox.last_error = None
                mailbox.consecutive_failures = 0
            else:
                mailbox.last_error = result.error
                mailbox.last_error_at = now
                mailbox.consecutive_failures += 1
                if mailbox.consecutive_failures in (1, 5, 20):  # nicht bei jedem Lauf ins Audit-Log
                    audit(db, self.actor, "mailbox.sync_failed", company_id=mailbox.company_id, entity_type="mailbox",
                          entity_id=mailbox.id, details={"error": result.error, "consecutive": mailbox.consecutive_failures})
            db.commit()

    def _sync_folder(self, server: MailServer, mailbox: Mailbox, company_slug: str, folder: str,
                     max_attachment: int, max_message: int) -> FolderResult:
        result = FolderResult(folder)
        info = server.select_folder(folder)
        with self.sessions() as db:
            state = db.scalar(select(MailboxSyncState).where(
                MailboxSyncState.mailbox_id == mailbox.id, MailboxSyncState.folder_name == folder).with_for_update())
            if state.uidvalidity is not None and state.uidvalidity != info.uidvalidity:
                # UIDs sind ungültig geworden → Backfill neu. Duplikate verhindert der Rohdaten-Hash.
                result.uidvalidity_changed = True
                audit(db, self.actor, "mailbox.uidvalidity_changed", company_id=mailbox.company_id, entity_type="mailbox",
                      entity_id=mailbox.id, details={"folder": folder, "old": state.uidvalidity, "new": info.uidvalidity})
                state.last_seen_uid = None
                state.backfill_completed_at = None
            state.uidvalidity = info.uidvalidity
            state_id, last_seen = state.id, state.last_seen_uid
            db.commit()

        start = self.settings.import_start_at
        if last_seen is None:
            # SINCE vergleicht nur das Datum in Server-Zeitzone → einen Tag Puffer, exakt gefiltert wird unten.
            uids = server.search_since(start.date() - timedelta(days=1))
        else:
            uids = server.search_uid_from(last_seen + 1)

        batch_size = max(1, self.settings.imap_fetch_batch_size)
        for i in range(0, len(uids), batch_size):
            batch = uids[i:i + batch_size]
            metas = server.fetch_meta(batch)
            for uid in batch:
                self._renew(mailbox.id)
                meta = metas.get(uid)
                if meta is None:  # zwischenzeitlich gelöscht
                    self._advance(state_id, uid)
                    continue
                outcome = self._import_one(server, mailbox, company_slug, state_id, folder, info.uidvalidity, meta,
                                           max_attachment, max_message)
                if outcome == "imported":
                    result.imported += 1
                elif outcome == "before_start":
                    result.before_start += 1
                else:
                    result.already_known += 1

        with self.sessions() as db:
            state = db.get(MailboxSyncState, state_id)
            now = utcnow()
            if state.backfill_completed_at is None:
                state.backfill_completed_at = now
            state.last_synced_at = now
            state.last_error = None
            db.commit()
        return result

    def _advance(self, state_id: int, uid: int, db: Session | None = None) -> None:
        def apply(s: Session) -> None:
            state = s.get(MailboxSyncState, state_id)
            if state.last_seen_uid is None or uid > state.last_seen_uid:
                state.last_seen_uid = uid

        if db is not None:
            apply(db)
            return
        with self.sessions() as s:
            apply(s)
            s.commit()

    def _import_one(self, server: MailServer, mailbox: Mailbox, company_slug: str, state_id: int, folder: str,
                    uidvalidity: int, meta: MessageMeta, max_attachment: int, max_message: int) -> str:
        if meta.internaldate.tzinfo is None:
            raise MailServerError(f"INTERNALDATE ohne Zeitzone (UID {meta.uid})")
        if meta.internaldate < self.settings.import_start_at:
            self._advance(state_id, meta.uid)
            return "before_start"

        with self.sessions() as db:
            known = db.scalar(select(EmailMessage.id).where(
                EmailMessage.mailbox_id == mailbox.id, EmailMessage.folder_name == folder,
                EmailMessage.uidvalidity == uidvalidity, EmailMessage.uid == meta.uid))
        if known:
            self._advance(state_id, meta.uid)
            return "known"

        values: dict = dict(
            company_id=mailbox.company_id, mailbox_id=mailbox.id, sync_state_id=state_id, folder_name=folder,
            uidvalidity=uidvalidity, uid=meta.uid, received_at=meta.internaldate, size_bytes=meta.size,
        )
        parsed: ParsedMessage | None = None
        stored_attachments: list[dict] = []

        if meta.size > max_message:
            values.update(status=EmailMessageStatus.manual_review,
                          review_reason=f"Nachricht größer als {max_message // (1024 * 1024)} MB – nicht heruntergeladen")
        else:
            raw = server.fetch_raw(meta.uid)
            raw_obj = self.store.put(company_slug, "raw_email", raw, "eml")
            with self.sessions() as db:
                same_raw = db.scalar(select(EmailMessage.id).where(
                    EmailMessage.mailbox_id == mailbox.id, EmailMessage.raw_sha256 == raw_obj.sha256).limit(1))
            if same_raw:
                self._advance(state_id, meta.uid)
                return "known"
            values.update(raw_sha256=raw_obj.sha256, raw_storage_key=raw_obj.key, size_bytes=len(raw))
            try:
                parsed = parse_message(raw, max_attachment_bytes=max_attachment)
            except Exception as exc:  # noqa: BLE001 – kaputte Nachricht: Original bleibt gespeichert, Prüfung manuell
                values.update(status=EmailMessageStatus.error, review_reason=f"MIME nicht lesbar: {type(exc).__name__}")
            if parsed is not None:
                for att in parsed.attachments:
                    row = dict(
                        company_id=mailbox.company_id, part_index=att.part_index,
                        filename=_clean(att.filename, 1000), declared_content_type=_clean(att.declared_content_type, 255),
                        detected_content_type=att.detected_content_type, size_bytes=att.size_bytes,
                        is_inline=att.is_inline, content_id=_clean(att.content_id, 998), safety_status=att.safety,
                        rejection_reason=att.rejection_reason,
                    )
                    if att.safety == AttachmentSafety.ok and att.data is not None:
                        obj = self.store.put(company_slug, "attachment", att.data, att.extension)
                        row.update(sha256=obj.sha256, storage_key=obj.key)
                    stored_attachments.append(row)
                values.update(
                    message_id_header=_clean(parsed.message_id, 998),
                    subject=_clean(parsed.subject, 10_000),
                    from_address=_clean(parsed.from_address, 998),
                    to_addresses=[a for a in (_clean(x, 998) for x in parsed.to_addresses) if a][:100],
                    header_date=parsed.header_date,
                    attachment_count=sum(1 for a in parsed.attachments if a.safety != AttachmentSafety.inline_skipped),
                    detected_invoice_links=[a for a in (_clean(u, 2000) for u in parsed.invoice_links) if a],
                    body_preview=_clean(parsed.body_preview, 2000),
                    body_mentions_invoice=parsed.body_mentions_invoice,
                )

        with self.sessions() as db:
            message_id = db.execute(
                insert(EmailMessage).values(**values)
                .on_conflict_do_nothing(constraint="uq_email_messages_imap_identity")
                .returning(EmailMessage.id)
            ).scalar()
            if message_id is None:
                self._advance(state_id, meta.uid, db)
                db.commit()
                return "known"
            for row in stored_attachments:
                db.add(EmailAttachment(email_message_id=message_id, **row))
            jobs.enqueue(db, company_id=mailbox.company_id, job_type=JobType.process_message,
                         dedup_key=f"process_message:{message_id}", payload={"email_message_id": message_id})
            self._advance(state_id, meta.uid, db)
            db.commit()
        return "imported"
