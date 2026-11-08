from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from invoiceflow.api.deps import AuthContext, get_db, require_admin, require_viewer
from invoiceflow.api.schemas import (
    AutoSendIn,
    ConfirmIn,
    DatevAddressIn,
    DatevDestinationOut,
    DatevSenderIn,
    GateStepIn,
    GateStepOut,
    ReleaseIn,
)
from invoiceflow.config import Settings, get_settings
from invoiceflow.db.models import DatevDestination
from invoiceflow.security.secrets import is_secret_configured
from invoiceflow.services import datev_gate as gate

router = APIRouter(prefix="/api/datev/destinations", tags=["datev"])


def _out(db: Session, dest: DatevDestination, settings: Settings) -> DatevDestinationOut:
    permission = gate.evaluate_send_permission(db, dest, settings)
    return DatevDestinationOut(
        id=dest.id,
        company_id=dest.company_id,
        channel=dest.channel,
        target_address=dest.target_address,
        address_format_warnings=gate.address_format_warnings(dest.target_address),
        sender_address=dest.sender_address,
        smtp_host=dest.smtp_host,
        smtp_port=dest.smtp_port,
        smtp_tls_mode=dest.smtp_tls_mode,
        smtp_username=dest.smtp_username,
        smtp_credential_ref=dest.smtp_credential_ref,
        smtp_credential_configured=bool(dest.smtp_credential_ref and is_secret_configured(dest.smtp_credential_ref)),
        gate_steps=[
            GateStepOut(key=k, label=label, done_at=getattr(dest, f"{k}_at"), done_by=getattr(dest, f"{k}_by"))
            for k, label in gate.GATE_STEPS.items()
        ],
        production_released_at=dest.production_released_at,
        production_released_by=dest.production_released_by,
        auto_send_enabled=dest.auto_send_enabled,
        send_allowed=permission.allowed,
        send_blockers=permission.reasons,
        test_mode=permission.test_mode,
        notes=dest.notes,
    )


def _load(db: Session, auth: AuthContext, dest_id: int) -> DatevDestination:
    # FOR UPDATE: zwei Admins können die Freigabe nicht gleichzeitig verändern.
    dest = db.scalar(select(DatevDestination).where(DatevDestination.id == dest_id).with_for_update())
    if dest is None or not auth.can_access(dest.company_id):
        raise HTTPException(404, "Nicht gefunden")
    return dest


def _apply(db: Session, dest: DatevDestination, settings: Settings, fn, *args) -> DatevDestinationOut:
    try:
        fn(*args)
    except (gate.GateError, ValueError) as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return _out(db, dest, settings)


@router.get("", response_model=list[DatevDestinationOut])
def list_destinations(
    auth: AuthContext = Depends(require_viewer), db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> list[DatevDestinationOut]:
    stmt = select(DatevDestination).order_by(DatevDestination.company_id, DatevDestination.channel)
    if auth.company_ids is not None:
        stmt = stmt.where(DatevDestination.company_id.in_(auth.company_ids))
    return [_out(db, d, settings) for d in db.scalars(stmt)]


@router.put("/{dest_id}/address", response_model=DatevDestinationOut)
def set_address(dest_id: int, body: DatevAddressIn, auth: AuthContext = Depends(require_admin),
                db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> DatevDestinationOut:
    dest = _load(db, auth, dest_id)
    return _apply(db, dest, settings, gate.set_target_address, db, dest, str(body.target_address), auth.actor)


@router.put("/{dest_id}/sender", response_model=DatevDestinationOut)
def set_sender(dest_id: int, body: DatevSenderIn, auth: AuthContext = Depends(require_admin),
               db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> DatevDestinationOut:
    dest = _load(db, auth, dest_id)

    def apply() -> None:
        gate.set_sender(db, dest, auth.actor, sender_address=str(body.sender_address), smtp_host=body.smtp_host,
                        smtp_port=body.smtp_port, smtp_tls_mode=body.smtp_tls_mode, smtp_username=body.smtp_username,
                        smtp_credential_ref=body.smtp_credential_ref)

    return _apply(db, dest, settings, apply)


@router.post("/{dest_id}/verify-address", response_model=DatevDestinationOut)
def verify_address(dest_id: int, body: ConfirmIn, auth: AuthContext = Depends(require_admin),
                   db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> DatevDestinationOut:
    dest = _load(db, auth, dest_id)
    return _apply(db, dest, settings, gate.verify_address, db, dest, body.confirmation, auth.actor)


@router.post("/{dest_id}/gate/{step}", response_model=DatevDestinationOut)
def confirm_step(dest_id: int, step: str, body: GateStepIn, auth: AuthContext = Depends(require_admin),
                 db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> DatevDestinationOut:
    dest = _load(db, auth, dest_id)
    return _apply(db, dest, settings, gate.confirm_step, db, dest, step, auth.actor, body.note)


@router.delete("/{dest_id}/gate/{step}", response_model=DatevDestinationOut)
def revoke_step(dest_id: int, step: str, auth: AuthContext = Depends(require_admin),
                db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> DatevDestinationOut:
    dest = _load(db, auth, dest_id)
    return _apply(db, dest, settings, gate.revoke_step, db, dest, step, auth.actor)


@router.post("/{dest_id}/release", response_model=DatevDestinationOut)
def release(dest_id: int, body: ReleaseIn, auth: AuthContext = Depends(require_admin),
            db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> DatevDestinationOut:
    dest = _load(db, auth, dest_id)
    return _apply(db, dest, settings, gate.release_production, db, dest, auth.actor, body.acknowledgement)


@router.post("/{dest_id}/auto-send", response_model=DatevDestinationOut)
def auto_send(dest_id: int, body: AutoSendIn, auth: AuthContext = Depends(require_admin),
              db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> DatevDestinationOut:
    dest = _load(db, auth, dest_id)
    return _apply(db, dest, settings, gate.set_auto_send, db, dest, body.enabled, auth.actor)
