"""Gemeinsame FastAPI-Abhängigkeiten: DB-Session, Authentifizierung, Rollen, Mandantenzugriff."""
from __future__ import annotations

import ipaddress
import secrets
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from invoiceflow.config import Settings, get_settings
from invoiceflow.db.enums import UserRole
from invoiceflow.db.models import User, UserCompanyAccess, UserSession
from invoiceflow.db.session import get_sessionmaker
from invoiceflow.security.sessions import load_session
from invoiceflow.services.audit import Actor

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_HEADER = "X-CSRF-Token"


def get_db() -> Iterator[Session]:
    db = get_sessionmaker()()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def client_ip(request: Request) -> str | None:
    # Railway hängt die echte Client-IP rechts an X-Forwarded-For an; Werte
    # weiter links kann der Client selbst setzen.
    forwarded = request.headers.get("x-forwarded-for")
    candidate = forwarded.split(",")[-1].strip() if forwarded else (request.client.host if request.client else None)
    try:
        return str(ipaddress.ip_address(candidate)) if candidate else None
    except ValueError:
        return None


@dataclass
class AuthContext:
    user: User
    session: UserSession
    ip: str | None
    company_ids: set[int] | None  # None = alle (Admin)

    @property
    def actor(self) -> Actor:
        return Actor.for_user(self.user, self.ip)

    @property
    def is_admin(self) -> bool:
        return self.user.role == UserRole.admin

    def can_access(self, company_id: int) -> bool:
        return self.company_ids is None or company_id in self.company_ids


def get_auth(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AuthContext:
    token = request.cookies.get(settings.session_cookie_name)
    loaded = load_session(db, token, settings) if token else None
    if loaded is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Nicht angemeldet")
    sess, user = loaded
    if request.method in UNSAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not secrets.compare_digest(sent, sess.csrf_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF-Token fehlt oder ist ungültig")
    company_ids: set[int] | None = None
    if user.role != UserRole.admin:
        company_ids = set(db.scalars(select(UserCompanyAccess.company_id).where(UserCompanyAccess.user_id == user.id)))
    db.commit()  # last_seen_at
    return AuthContext(user=user, session=sess, ip=client_ip(request), company_ids=company_ids)


def require_role(*roles: UserRole):
    def dependency(auth: AuthContext = Depends(get_auth)) -> AuthContext:
        if auth.user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Keine Berechtigung")
        if auth.user.must_change_password:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Bitte zuerst das Passwort ändern")
        return auth

    return dependency


require_admin = require_role(UserRole.admin)
require_reviewer = require_role(UserRole.admin, UserRole.reviewer)
require_viewer = require_role(UserRole.admin, UserRole.reviewer, UserRole.viewer)


def ensure_company(auth: AuthContext, company_id: int) -> None:
    """404 statt 403, damit fremde Unternehmen nicht einmal als existent erkennbar sind."""
    if not auth.can_access(company_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Nicht gefunden")
