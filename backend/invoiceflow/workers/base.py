"""Gemeinsame Worker-Infrastruktur: Identität, Heartbeat, sauberes Beenden."""
from __future__ import annotations

import functools
import os
import shutil
import signal
import socket
import subprocess
import threading
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from invoiceflow.db.models import WorkerHeartbeat


def make_worker_id(kind: str) -> str:
    # RAILWAY_REPLICA_ID unterscheidet Replikas; Hostname+PID als Fallback.
    replica = os.environ.get("RAILWAY_REPLICA_ID") or socket.gethostname()
    return f"{kind}:{replica}:{os.getpid()}"


@functools.cache
def _pg_dump_version() -> str | None:
    if not shutil.which("pg_dump"):
        return None
    try:
        out = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    parts = out.split()  # "pg_dump (PostgreSQL) 18.1 (Debian ...)"
    return parts[2] if len(parts) >= 3 else out.strip() or None


def capabilities() -> dict[str, Any]:
    """Was dieser Prozess kann – ohne Geheimnisse preiszugeben (nur vorhanden/nicht vorhanden)."""
    return {
        "ai_key": bool((os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or "").strip()),
        "tesseract": shutil.which("tesseract") is not None,
        "pg_dump": _pg_dump_version(),
        "commit": (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "")[:7] or None,
    }


def heartbeat(db: Session, worker_id: str, worker_type: str, started_at: datetime, details: dict[str, Any]) -> None:
    now = datetime.now(UTC)
    details = {**details, "env": capabilities()}
    stmt = insert(WorkerHeartbeat).values(
        worker_id=worker_id, worker_type=worker_type, started_at=started_at, last_beat_at=now, details=details
    )
    db.execute(stmt.on_conflict_do_update(
        index_elements=[WorkerHeartbeat.worker_id],
        set_={"last_beat_at": now, "details": stmt.excluded.details},
    ))
    db.commit()


def remove_heartbeat(sessions: sessionmaker[Session], worker_id: str) -> None:
    """Sauber beendeter Worker (z. B. Ablösung durch einen neuen Deploy) verschwindet aus der Übersicht.

    Abgestürzte Worker behalten ihren Eintrag – deren fehlende Rückmeldung soll auffallen.
    """
    try:
        with sessions() as db:
            db.execute(delete(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == worker_id))
            db.commit()
    except Exception:  # noqa: BLE001 – beim Herunterfahren nicht mehr wichtig
        pass


class StopSignal:
    """SIGTERM (Railway-Deploy/Neustart) beendet den Worker nach der laufenden Nachricht."""

    def __init__(self) -> None:
        self.event = threading.Event()

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.event.set())

    @property
    def stopped(self) -> bool:
        return self.event.is_set()

    def wait(self, seconds: float) -> None:
        self.event.wait(seconds)
