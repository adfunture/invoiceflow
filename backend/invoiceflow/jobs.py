"""Job-Warteschlange in PostgreSQL (``processing_jobs``).

* ``enqueue`` ist idempotent über ``dedup_key``.
* ``claim`` nutzt ``FOR UPDATE SKIP LOCKED``: Parallele Worker bekommen nie
  denselben Job. Jobs von abgestürzten Workern werden nach Ablauf des Lease
  erneut vergeben.
* Nach ``max_attempts`` Fehlversuchen landet ein Job auf ``dead`` und wird
  im Dashboard angezeigt statt endlos wiederholt.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from invoiceflow.db.enums import JobStatus, JobType
from invoiceflow.db.models import ProcessingJob

MAX_BACKOFF = timedelta(hours=1)


def utcnow() -> datetime:
    return datetime.now(UTC)


def enqueue(
    db: Session,
    *,
    company_id: int,
    job_type: JobType,
    dedup_key: str,
    payload: dict[str, Any],
    priority: int = 100,
    max_attempts: int = 5,
    run_after: datetime | None = None,
) -> bool:
    """True, wenn der Job neu angelegt wurde."""
    stmt = (
        insert(ProcessingJob)
        .values(company_id=company_id, job_type=job_type, dedup_key=dedup_key, payload=payload, priority=priority,
                max_attempts=max_attempts, run_after=run_after or utcnow())
        .on_conflict_do_nothing(constraint="uq_processing_jobs_dedup_key")
        .returning(ProcessingJob.id)
    )
    return db.execute(stmt).scalar() is not None


def claim(db: Session, job_type: JobType, worker_id: str, lease_seconds: int, now: datetime | None = None) -> ProcessingJob | None:
    """Holt den nächsten fälligen Job und committet die Übernahme."""
    now = now or utcnow()
    while True:
        job = db.scalar(
            select(ProcessingJob)
            .where(
                ProcessingJob.job_type == job_type,
                or_(
                    and_(ProcessingJob.status == JobStatus.queued, ProcessingJob.run_after <= now),
                    and_(ProcessingJob.status == JobStatus.running, ProcessingJob.lease_expires_at < now),
                ),
            )
            .order_by(ProcessingJob.priority, ProcessingJob.run_after, ProcessingJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            db.commit()
            return None
        if job.status == JobStatus.running:
            job.last_error = f"Lease von {job.locked_by} abgelaufen – Job wird neu vergeben"
        if job.attempts >= job.max_attempts:
            job.status = JobStatus.dead
            job.finished_at = now
            job.locked_by = job.lease_expires_at = None
            db.commit()
            continue
        job.status = JobStatus.running
        job.attempts += 1
        job.locked_by = worker_id
        job.locked_at = now
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        db.commit()
        return job


def extend_lease(db: Session, job_id: int, worker_id: str, lease_seconds: int) -> bool:
    result = db.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == job_id, ProcessingJob.locked_by == worker_id, ProcessingJob.status == JobStatus.running)
        .values(lease_expires_at=utcnow() + timedelta(seconds=lease_seconds))
    )
    return result.rowcount == 1


def complete(db: Session, job_id: int, worker_id: str) -> bool:
    """False, wenn der Job inzwischen einem anderen Worker gehört (Lease verloren)."""
    result = db.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == job_id, ProcessingJob.locked_by == worker_id, ProcessingJob.status == JobStatus.running)
        .values(status=JobStatus.succeeded, finished_at=utcnow(), lease_expires_at=None, last_error=None)
    )
    return result.rowcount == 1


def fail(db: Session, job_id: int, worker_id: str, error: str, *, retry: bool = True) -> JobStatus | None:
    job = db.scalar(select(ProcessingJob).where(ProcessingJob.id == job_id).with_for_update())
    if job is None or job.locked_by != worker_id or job.status != JobStatus.running:
        return None
    job.last_error = error[:4000]
    job.lease_expires_at = None
    job.locked_by = None
    if retry and job.attempts < job.max_attempts:
        job.status = JobStatus.queued
        job.run_after = utcnow() + min(timedelta(seconds=30 * 2 ** job.attempts), MAX_BACKOFF)
    else:
        job.status = JobStatus.dead
        job.finished_at = utcnow()
    return job.status
