"""Railway-Service ``worker-imap``: ruft alle aktiven Postfächer regelmäßig ab.

Ein Postfach ist fällig, wenn sein letzter Abruf länger als
``IMAP_POLL_INTERVAL_SECONDS`` (Standard 5 Minuten) zurückliegt oder im
Dashboard „Jetzt abrufen“ gedrückt wurde. Bei wiederholten Fehlern wird das
Intervall verlängert (max. 1 Stunde), damit ein gesperrtes Konto nicht
dauernd angefragt wird.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from invoiceflow.config import Settings
from invoiceflow.db.models import Mailbox
from invoiceflow.mail.imap import MailServerFactory, default_factory
from invoiceflow.mail.sync import MailboxSyncer
from invoiceflow.storage import DocumentStore
from invoiceflow.workers.base import StopSignal, heartbeat, make_worker_id, remove_heartbeat

log = logging.getLogger("invoiceflow.worker.imap")
TICK_SECONDS = 15
MAX_BACKOFF = timedelta(hours=1)


def due_mailbox_ids(db: Session, settings: Settings, now: datetime) -> list[int]:
    interval = timedelta(seconds=settings.imap_poll_interval_seconds)
    due: list[int] = []
    for mb in db.scalars(select(Mailbox).where(Mailbox.is_active.is_(True)).order_by(Mailbox.id)):
        if mb.sync_requested_at is not None or mb.last_sync_started_at is None:
            due.append(mb.id)
            continue
        wait = min(interval * (2 ** min(mb.consecutive_failures, 6)), max(interval, MAX_BACKOFF))
        if mb.last_sync_started_at + wait <= now:
            due.append(mb.id)
    return due


def run_once(syncer: MailboxSyncer, sessions: sessionmaker[Session], settings: Settings, stop: StopSignal | None = None) -> dict[int, str]:
    with sessions() as db:
        ids = due_mailbox_ids(db, settings, datetime.now(UTC))
    results: dict[int, str] = {}
    for mailbox_id in ids:
        if stop is not None and stop.stopped:
            break
        results[mailbox_id] = syncer.sync_mailbox(mailbox_id).status
    return results


def run_forever(sessions: sessionmaker[Session], store: DocumentStore, settings: Settings,
                server_factory: MailServerFactory = default_factory) -> None:
    stop = StopSignal()
    stop.install()
    worker_id = make_worker_id("imap")
    started = datetime.now(UTC)
    syncer = MailboxSyncer(sessions, store, settings, server_factory, worker_id, sleep=stop.wait)
    log.info("worker_started", extra={"worker_id": worker_id, "import_start_at": settings.import_start_at.isoformat()})
    last = {}
    while not stop.stopped:
        try:
            with sessions() as db:
                heartbeat(db, worker_id, "imap", started, {"last_results": {str(k): v for k, v in last.items()}})
            last = run_once(syncer, sessions, settings, stop) or last
        except Exception:  # noqa: BLE001 – z. B. DB kurz weg: weiterlaufen, Railway-Restart nicht nötig
            log.exception("worker_tick_failed", extra={"worker_id": worker_id})
        stop.wait(TICK_SECONDS)
    remove_heartbeat(sessions, worker_id)
    log.info("worker_stopped", extra={"worker_id": worker_id})
