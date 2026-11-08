from __future__ import annotations

from fastapi import APIRouter, Response
from sqlalchemy import text

from invoiceflow.api.schemas import HealthOut
from invoiceflow.db.session import get_engine

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
def liveness() -> HealthOut:
    """Prozess lebt. Prüft bewusst keine Abhängigkeiten."""
    return HealthOut(status="ok", checks={"process": "ok"})


@router.get("/health/ready", response_model=HealthOut)
def readiness(response: Response) -> HealthOut:
    """Railway-Healthcheck: Datenbank erreichbar und Migrationen auf Stand."""
    checks: dict[str, str] = {}
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        checks["database"] = "ok"
        checks["schema"] = _schema_status(version)
    except Exception as exc:  # noqa: BLE001 – Healthcheck meldet jeden Fehler als degraded
        checks["database"] = f"error: {type(exc).__name__}"
    status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    if status != "ok":
        response.status_code = 503
    return HealthOut(status=status, checks=checks)


def _schema_status(current: str | None) -> str:
    from alembic.script import ScriptDirectory
    from alembic.config import Config

    from invoiceflow.cli import _alembic_ini

    head = ScriptDirectory.from_config(Config(str(_alembic_ini()))).get_current_head()
    return "ok" if current == head else f"outdated ({current} != {head})"
