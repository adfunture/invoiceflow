from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from invoiceflow.api.deps import AuthContext, client_ip, get_auth, get_db
from invoiceflow.api.schemas import ChangePasswordIn, LoginIn, SessionOut, UserOut
from invoiceflow.config import Settings, get_settings
from invoiceflow.db.enums import ActorType, UserRole
from invoiceflow.db.models import User, UserCompanyAccess
from invoiceflow.security import passwords
from invoiceflow.security.sessions import (
    create_session,
    is_login_blocked,
    record_login_attempt,
    revoke_all_sessions,
    revoke_session,
)
from invoiceflow.services.audit import Actor, audit

router = APIRouter(prefix="/api/auth", tags=["auth"])

_INVALID = "E-Mail oder Passwort falsch"


def _set_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_absolute_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )


@router.post("/login", response_model=SessionOut)
def login(
    body: LoginIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SessionOut:
    email = body.email.lower()
    ip = client_ip(request)
    if is_login_blocked(db, email, ip, settings):
        audit(db, Actor(type=ActorType.system, label=email, ip=ip), "auth.login_blocked", details={"email": email})
        db.commit()
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Zu viele Fehlversuche. Bitte später erneut versuchen.")

    user = db.scalar(select(User).where(User.email == email))
    ok = passwords.verify_password(user.password_hash if user else None, body.password)
    if not ok or user is None or not user.is_active:
        record_login_attempt(db, email, ip, False)
        audit(db, Actor(type=ActorType.system, label=email, ip=ip), "auth.login_failed", details={"email": email})
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _INVALID)

    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(body.password)
    record_login_attempt(db, email, ip, True)
    user.last_login_at = datetime.now(UTC)
    issued = create_session(db, user, settings, ip=ip, user_agent=request.headers.get("user-agent"))
    audit(db, Actor.for_user(user, ip), "auth.login")
    db.commit()
    company_ids = None
    if user.role != UserRole.admin:
        company_ids = sorted(db.scalars(select(UserCompanyAccess.company_id).where(UserCompanyAccess.user_id == user.id)))
    _set_cookie(response, settings, issued.token)
    return SessionOut(user=UserOut.model_validate(user), csrf_token=issued.csrf_token, company_ids=company_ids)


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    auth: AuthContext = Depends(get_auth),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> None:
    revoke_session(db, auth.session.id)
    audit(db, auth.actor, "auth.logout")
    db.commit()
    response.delete_cookie(settings.session_cookie_name, path="/")


@router.get("/me", response_model=SessionOut)
def me(auth: AuthContext = Depends(get_auth)) -> SessionOut:
    return SessionOut(
        user=UserOut.model_validate(auth.user),
        csrf_token=auth.session.csrf_token,
        company_ids=None if auth.company_ids is None else sorted(auth.company_ids),
    )


@router.post("/change-password", status_code=204)
def change_password(
    body: ChangePasswordIn,
    auth: AuthContext = Depends(get_auth),
    db: Session = Depends(get_db),
) -> None:
    user = db.get(User, auth.user.id)
    if not passwords.verify_password(user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Aktuelles Passwort ist falsch")
    try:
        user.password_hash = passwords.hash_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    user.must_change_password = False
    revoke_all_sessions(db, user.id, except_session_id=auth.session.id)
    audit(db, auth.actor, "auth.password_changed", entity_type="user", entity_id=user.id)
    db.commit()
