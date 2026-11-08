"""Technische Aufräumarbeiten (keine Geschäftsdaten): Anmeldeversuche, Sitzungen, Jobs, Heartbeats."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_
from sqlalchemy.orm import Session

from invoiceflow.db.enums import JobStatus
from invoiceflow.db.models import AlertState, LoginAttempt, ProcessingJob, UserSession, WorkerHeartbeat


def run_maintenance(db: Session, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    result = {
        "login_attempts": db.execute(delete(LoginAttempt).where(LoginAttempt.attempted_at < now - timedelta(days=90))).rowcount,
        "user_sessions": db.execute(delete(UserSession).where(or_(
            UserSession.expires_at < now - timedelta(days=30),
            UserSession.revoked_at < now - timedelta(days=30),
        ))).rowcount,
        # Nur erfolgreich abgeschlossene Jobs; fehlgeschlagene bleiben sichtbar.
        "processing_jobs": db.execute(delete(ProcessingJob).where(
            ProcessingJob.status == JobStatus.succeeded, ProcessingJob.finished_at < now - timedelta(days=30))).rowcount,
        "worker_heartbeats": db.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.last_beat_at < now - timedelta(days=7))).rowcount,
        "alert_states": db.execute(delete(AlertState).where(
            AlertState.active.is_(False), AlertState.resolved_at < now - timedelta(days=90))).rowcount,
    }
    return result
