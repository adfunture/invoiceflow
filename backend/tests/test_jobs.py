from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from invoiceflow import jobs
from invoiceflow.db.enums import JobStatus, JobType
from invoiceflow.db.models import ProcessingJob
from invoiceflow.db.session import get_sessionmaker

from .conftest import company_id

T = JobType.process_message


def _enqueue(key: str = "process_message:1", **kw) -> bool:
    with get_sessionmaker()() as s:
        created = jobs.enqueue(s, company_id=company_id("adfunture", s), job_type=T, dedup_key=key, payload={"x": 1}, **kw)
        s.commit()
        return created


def _claim(worker: str, lease: int = 60, now=None):
    with get_sessionmaker()() as s:
        job = jobs.claim(s, T, worker, lease, now=now)
        return None if job is None else (job.id, job.attempts)


def _job(job_id: int) -> ProcessingJob:
    with get_sessionmaker()() as s:
        return s.get(ProcessingJob, job_id)


def test_enqueue_is_idempotent():
    assert _enqueue() is True
    assert _enqueue() is False


def test_claim_and_complete():
    _enqueue()
    job_id, attempts = _claim("w1")
    assert attempts == 1
    assert _claim("w2") is None, "laufender Job wird nicht doppelt vergeben"
    with get_sessionmaker()() as s:
        assert jobs.complete(s, job_id, "w1")
        s.commit()
    assert _job(job_id).status == JobStatus.succeeded


def test_11_expired_lease_is_reclaimed_after_worker_restart():
    _enqueue()
    job_id, _ = _claim("worker-vor-neustart", lease=60)
    later = datetime.now(UTC) + timedelta(minutes=2)
    reclaimed = _claim("worker-nach-neustart", now=later)
    assert reclaimed == (job_id, 2)
    assert "abgelaufen" in _job(job_id).last_error
    with get_sessionmaker()() as s:  # der alte Worker darf nicht mehr abschließen
        assert jobs.complete(s, job_id, "worker-vor-neustart") is False


def test_failure_backoff_and_dead_letter():
    _enqueue(max_attempts=2)
    job_id, _ = _claim("w")
    with get_sessionmaker()() as s:
        assert jobs.fail(s, job_id, "w", "Fehler 1") == JobStatus.queued
        s.commit()
    assert _job(job_id).run_after > datetime.now(UTC)
    assert _claim("w") is None, "Backoff: noch nicht fällig"
    _, attempts = _claim("w", now=datetime.now(UTC) + timedelta(hours=2))
    assert attempts == 2
    with get_sessionmaker()() as s:
        assert jobs.fail(s, job_id, "w", "Fehler 2") == JobStatus.dead
        s.commit()


def test_crash_loop_ends_in_dead_letter():
    _enqueue(max_attempts=2)
    now = datetime.now(UTC)
    assert _claim("a", lease=1, now=now) is not None
    assert _claim("b", lease=1, now=now + timedelta(minutes=1)) is not None
    assert _claim("c", lease=1, now=now + timedelta(minutes=2)) is None
    with get_sessionmaker()() as s:
        assert s.scalar(select(ProcessingJob.status)) == JobStatus.dead


@pytest.mark.skipif(int(os.environ.get("TEST_DB_POOL_SIZE", "1")) < 2,
                    reason="braucht echtes PostgreSQL mit mehreren Verbindungen (CI)")
def test_17_parallel_workers_never_get_the_same_job():
    for i in range(20):
        _enqueue(f"process_message:{i}")
    claimed: list[int] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        while True:
            got = _claim(name)
            if got is None:
                return
            with lock:
                claimed.append(got[0])

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(set(claimed)) and len(claimed) == 20
