"""Railway-Service ``worker-datev``: legt Übertragungen an und sendet freigegebene Belege.

Genau **eine** Replika betreiben. Mehrere wären durch ``SKIP LOCKED`` und die
Unique-Indizes zwar sicher, bringen bei DATEV Upload Mail aber keinen Vorteil.
Bei SIGTERM wird der laufende Versand zu Ende geführt (``drainingSeconds``).
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from invoiceflow.datev.sender import DatevSender
from invoiceflow.workers.base import StopSignal, heartbeat, remove_heartbeat

log = logging.getLogger("invoiceflow.worker.datev")
TICK_SECONDS = 30


def run_forever(sender: DatevSender) -> None:
    stop = StopSignal()
    stop.install()
    started = datetime.now(UTC)
    log.info("worker_started", extra={"worker_id": sender.worker_id,
                                      "real_send_allowed_by_env": sender.settings.datev_allow_real_send})
    last: dict = {}
    while not stop.stopped:
        try:
            with sender.sessions() as db:
                heartbeat(db, sender.worker_id, "datev", started, {"last_cycle": last})
            result = sender.run_cycle()
            last = vars(result)
        except Exception:  # noqa: BLE001 – Worker läuft weiter; einzelne Übertragungen sind abgesichert
            log.exception("worker_tick_failed", extra={"worker_id": sender.worker_id})
        stop.wait(TICK_SECONDS)
    remove_heartbeat(sender.sessions, sender.worker_id)
    log.info("worker_stopped", extra={"worker_id": sender.worker_id})
