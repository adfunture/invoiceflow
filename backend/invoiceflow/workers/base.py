"""Gemeinsame Worker-Infrastruktur: Identität, Heartbeat, sauberes Beenden."""
from __future__ import annotations

import os
import signal
import socket
import threading
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from invoiceflow.db.models import WorkerHeartbeat


def make_worker_id(kind: str) -> str:
    # RAILWAY_REPLICA_ID unterscheidet Replikas; Hostname+PID als Fallback.
    replica = os.environ.get("RAILWAY_REPLICA_ID") or socket.gethostname()
    return f"{kind}:{replica}:{os.getpid()}"


def heartbeat(db: Session, worker_id: str, worker_type: str, started_at: datetime, details: dict[str, Any]) -> None:
    now = datetime.now(UTC)
    stmt = insert(WorkerHeartbeat).values(
        worker_id=worker_id, worker_type=worker_type, started_at=started_at, last_beat_at=now, details=details
    )
    db.execute(stmt.on_conflict_do_update(
        index_elements=[WorkerHeartbeat.worker_id],
        set_={"last_beat_at": now, "details": stmt.excluded.details},
    ))
    db.commit()


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
