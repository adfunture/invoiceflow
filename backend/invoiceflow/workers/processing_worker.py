"""Railway-Service ``worker-processing``: verarbeitet ``process_message``-Jobs.

Mehrere Instanzen sind möglich: Jobs werden per ``SKIP LOCKED`` verteilt,
Dubletten werden in der Pipeline unter Advisory-Locks entschieden. Stirbt
ein Worker, läuft der Job-Lease ab und ein anderer übernimmt; Dokumente in
nicht-terminalem Status werden dabei erneut ausgewertet.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from invoiceflow import jobs
from invoiceflow.db.enums import JobType
from invoiceflow.logging_setup import redact_text
from invoiceflow.recognition.pipeline import DocumentProcessor
from invoiceflow.workers.base import StopSignal, heartbeat, make_worker_id, remove_heartbeat

log = logging.getLogger("invoiceflow.worker.processing")
LEASE_SECONDS = 600
IDLE_SECONDS = 5


def run_once(processor: DocumentProcessor, sessions: sessionmaker[Session], worker_id: str,
             stop: StopSignal | None = None, max_jobs: int = 50) -> int:
    done = 0
    while done < max_jobs and not (stop and stop.stopped):
        with sessions() as db:
            job = jobs.claim(db, JobType.process_message, worker_id, LEASE_SECONDS)
        if job is None:
            break
        try:
            processor.process_message(int(job.payload["email_message_id"]))
            with sessions() as db:
                jobs.complete(db, job.id, worker_id)
                db.commit()
        except Exception as exc:  # noqa: BLE001 – Job wird mit Backoff wiederholt, danach Dead-Letter
            log.exception("job_failed", extra={"job_id": job.id})
            with sessions() as db:
                jobs.fail(db, job.id, worker_id, redact_text(f"{type(exc).__name__}: {exc}"))
                db.commit()
        done += 1
    return done


def run_forever(processor: DocumentProcessor, sessions: sessionmaker[Session]) -> None:
    stop = StopSignal()
    stop.install()
    worker_id = make_worker_id("processing")
    started = datetime.now(UTC)
    total = 0
    log.info("worker_started", extra={"worker_id": worker_id})
    while not stop.stopped:
        try:
            with sessions() as db:
                heartbeat(db, worker_id, "processing", started, {"processed_jobs": total})
            count = run_once(processor, sessions, worker_id, stop)
            total += count
            if count == 0:
                stop.wait(IDLE_SECONDS)
        except Exception:  # noqa: BLE001
            log.exception("worker_tick_failed", extra={"worker_id": worker_id})
            stop.wait(IDLE_SECONDS)
    remove_heartbeat(sessions, worker_id)
    log.info("worker_stopped", extra={"worker_id": worker_id})
