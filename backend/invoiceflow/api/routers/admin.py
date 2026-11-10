"""Einstellungen und Audit-Log."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from invoiceflow.api.deps import AuthContext, ensure_company, get_db, require_admin, require_viewer
from invoiceflow.api.schemas import AuditOut, SettingIn, SettingOut
from invoiceflow.config import Settings, get_settings
from invoiceflow.db.models import AuditLog, Company
from invoiceflow.services import app_settings
from invoiceflow.services.audit import audit

router = APIRouter(prefix="/api", tags=["admin"])


class AlertChannelOut(BaseModel):
    name: str
    ok: bool
    error: str | None = None


class TestAlertOut(BaseModel):
    channels: list[AlertChannelOut]


@router.post("/alerts/test", response_model=TestAlertOut)
def send_test_alert(auth: AuthContext = Depends(require_admin), db: Session = Depends(get_db),
                    settings: Settings = Depends(get_settings)) -> TestAlertOut:
    """Testalarm über alle eingerichteten Kanäle (E-Mail/Telegram) senden – prüft die ALERT_*-Konfiguration."""
    from invoiceflow.logging_setup import redact_text
    from invoiceflow.ops.monitor import configured_notifiers

    channels: list[AlertChannelOut] = []
    for notifier in configured_notifiers(settings):
        try:
            notifier.send("[InvoiceFlow] Testalarm", "Testalarm aus dem Dashboard – die Alarmierung funktioniert.")
            channels.append(AlertChannelOut(name=notifier.name, ok=True))
        except Exception as exc:  # noqa: BLE001 – Fehler gehen an die Oberfläche
            channels.append(AlertChannelOut(name=notifier.name, ok=False, error=redact_text(f"{type(exc).__name__}: {exc}")[:300]))
    audit(db, auth.actor, "alerts.test_sent", details={"channels": [c.model_dump() for c in channels]})
    db.commit()
    return TestAlertOut(channels=channels)


@router.get("/settings", response_model=list[SettingOut])
def list_settings(
    company_id: int | None = Query(default=None),
    auth: AuthContext = Depends(require_viewer),
    db: Session = Depends(get_db),
) -> list[SettingOut]:
    if company_id is not None:
        ensure_company(auth, company_id)
    return [
        SettingOut(key=key, value=app_settings.get_setting(db, key, company_id), default=spec.default,
                   description=spec.description, company_id=company_id)
        for key, spec in app_settings.SETTINGS.items()
    ]


@router.put("/settings/{key}", response_model=SettingOut)
def update_setting(key: str, body: SettingIn, auth: AuthContext = Depends(require_admin), db: Session = Depends(get_db)) -> SettingOut:
    spec = app_settings.SETTINGS.get(key)
    if spec is None:
        raise HTTPException(404, "Unbekannte Einstellung")
    if body.company_id is not None and db.get(Company, body.company_id) is None:
        raise HTTPException(404, "Unternehmen nicht gefunden")
    old = app_settings.get_setting(db, key, body.company_id)
    try:
        value = app_settings.set_setting(db, key, body.value, company_id=body.company_id, user_id=auth.user.id)
    except ValueError as exc:
        raise HTTPException(422, f"{key}: {exc}") from exc
    audit(db, auth.actor, "setting.changed", company_id=body.company_id, entity_type="setting", entity_id=key,
          details={"old": old, "new": value})
    db.commit()
    return SettingOut(key=key, value=value, default=spec.default, description=spec.description, company_id=body.company_id)


@router.get("/audit-logs", response_model=list[AuditOut])
def list_audit_logs(
    company_id: int | None = Query(default=None),
    entity_type: str | None = Query(default=None, max_length=100),
    before: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    auth: AuthContext = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).limit(limit)
    if company_id is not None:
        stmt = stmt.where(AuditLog.company_id == company_id)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if before:
        stmt = stmt.where(AuditLog.occurred_at < before)
    return list(db.scalars(stmt))
