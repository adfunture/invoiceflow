"""Audit-Logging. Wird in derselben Transaktion wie die Änderung geschrieben."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from invoiceflow.db.enums import ActorType
from invoiceflow.db.models import AuditLog, User
from invoiceflow.logging_setup import redact_value


@dataclass(frozen=True)
class Actor:
    type: ActorType
    user_id: int | None = None
    label: str | None = None
    ip: str | None = None

    @classmethod
    def for_user(cls, user: User, ip: str | None = None) -> "Actor":
        return cls(ActorType.user, user.id, user.email, ip)

    @classmethod
    def system(cls, label: str = "system") -> "Actor":
        return cls(ActorType.system, None, label)

    @classmethod
    def worker(cls, label: str) -> "Actor":
        return cls(ActorType.worker, None, label)


def audit(
    db: Session,
    actor: Actor,
    action: str,
    *,
    company_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            company_id=company_id,
            actor_type=actor.type,
            actor_user_id=actor.user_id,
            actor_label=actor.label,
            action=action,
            entity_type=entity_type,
            entity_id=None if entity_id is None else str(entity_id),
            details=redact_value("details", details or {}),
            ip_address=actor.ip,
        )
    )
