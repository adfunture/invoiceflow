"""Server-seitige Sessions und Brute-Force-Schutz."""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from invoiceflow.config import Settings
from invoiceflow.db.models import LoginAttempt, User, UserSession


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class IssuedSession:
    token: str  # nur im Cookie, nie in der DB
    csrf_token: str
    session: UserSession


def create_session(db: Session, user: User, settings: Settings, *, ip: str | None, user_agent: str | None) -> IssuedSession:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    row = UserSession(
        user_id=user.id,
        token_hash=_hash_token(token),
        csrf_token=csrf,
        expires_at=utcnow() + timedelta(hours=settings.session_absolute_hours),
        ip_address=ip,
        user_agent=(user_agent or "")[:500] or None,
    )
    db.add(row)
    db.flush()
    return IssuedSession(token=token, csrf_token=csrf, session=row)


def load_session(db: Session, token: str, settings: Settings) -> tuple[UserSession, User] | None:
    """Gültige Session + aktiver Benutzer oder None. Verlängert last_seen_at."""
    now = utcnow()
    row = db.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.token_hash == _hash_token(token))
    ).first()
    if row is None:
        return None
    sess, user = row
    idle_limit = sess.last_seen_at + timedelta(minutes=settings.session_idle_minutes)
    if sess.revoked_at is not None or sess.expires_at <= now or idle_limit <= now or not user.is_active:
        return None
    sess.last_seen_at = now
    return sess, user


def revoke_session(db: Session, session_id: int) -> None:
    db.execute(update(UserSession).where(UserSession.id == session_id).values(revoked_at=utcnow()))


def revoke_all_sessions(db: Session, user_id: int, *, except_session_id: int | None = None) -> None:
    stmt = update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
    if except_session_id is not None:
        stmt = stmt.where(UserSession.id != except_session_id)
    db.execute(stmt.values(revoked_at=utcnow()))


def is_login_blocked(db: Session, email: str, ip: str | None, settings: Settings) -> bool:
    since = utcnow() - timedelta(minutes=settings.login_window_minutes)
    by_account = db.scalar(
        select(func.count()).select_from(LoginAttempt).where(
            LoginAttempt.email == email, LoginAttempt.succeeded.is_(False), LoginAttempt.attempted_at >= since
        )
    )
    if by_account >= settings.login_max_failures_per_account:
        return True
    if ip:
        by_ip = db.scalar(
            select(func.count()).select_from(LoginAttempt).where(
                LoginAttempt.ip_address == ip, LoginAttempt.succeeded.is_(False), LoginAttempt.attempted_at >= since
            )
        )
        if by_ip >= settings.login_max_failures_per_ip:
            return True
    return False


def record_login_attempt(db: Session, email: str, ip: str | None, succeeded: bool) -> None:
    db.add(LoginAttempt(email=email, ip_address=ip, succeeded=succeeded))
