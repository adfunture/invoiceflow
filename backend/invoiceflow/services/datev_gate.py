"""Produktivfreigabe für den DATEV-Versand je Unternehmen.

Reihenfolge der Freigabe:
  1. address_verified       – Admin tippt die Adresse aus DATEV erneut ein
  2. sender_authorized      – SMTP-Absender ist bei DATEV autorisiert
  3. assignment_tested      – Unternehmenszuordnung getestet
  4. duplicate_check_tested – Dublettenprüfung getestet
  5. test_delivery_confirmed – Testrechnungen sind in DATEV angekommen
  6. production_released    – ausdrückliche Freigabe
Erst danach kann ``auto_send_enabled`` gesetzt werden. Die Datenbank erzwingt
5→6→auto_send zusätzlich per CHECK-Constraint.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Literal
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from invoiceflow.config import Settings
from invoiceflow.db.enums import DatevChannel
from invoiceflow.db.models import DatevDestination
from invoiceflow.security.secrets import is_secret_configured, validate_credential_ref
from invoiceflow.services import app_settings
from invoiceflow.services.audit import Actor, audit

GATE_STEPS: dict[str, str] = {
    "address_verified": "DATEV-Zieladresse verifiziert",
    "sender_authorized": "SMTP-Absender bei DATEV autorisiert",
    "assignment_tested": "Unternehmenszuordnung getestet",
    "duplicate_check_tested": "Dublettenprüfung getestet",
    "test_delivery_confirmed": "Testrechnungen in DATEV angekommen",
}
CONFIRMABLE_STEPS = tuple(k for k in GATE_STEPS if k != "address_verified")
UPLOAD_MAIL_DOMAIN = "uploadmail.datev.de"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


class GateError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def address_format_warnings(address: str | None) -> list[str]:
    """Plausibilitätsprüfung einer DATEV-Upload-Mail-Adresse (ersetzt nicht die Verifizierung)."""
    if not address:
        return ["Keine Zieladresse hinterlegt"]
    warnings: list[str] = []
    local, _, domain = address.partition("@")
    if domain != UPLOAD_MAIL_DOMAIN:
        warnings.append(f"Domain ist nicht {UPLOAD_MAIL_DOMAIN}")
    try:
        if str(uuid.UUID(local)) != local:
            raise ValueError
    except ValueError:
        warnings.append(
            "Lokaler Teil ist keine UUID im Format 8-4-4-4-12. Bitte die Adresse direkt in "
            "DATEV Unternehmen online prüfen (Abschreibfehler aus Screenshot?)."
        )
    return warnings


def _reset_gate_from(dest: DatevDestination, first_step: str) -> None:
    steps = list(GATE_STEPS)
    for step in steps[steps.index(first_step):]:
        setattr(dest, f"{step}_at", None)
        setattr(dest, f"{step}_by", None)
    dest.production_released_at = None
    dest.production_released_by = None
    dest.auto_send_enabled = False


def _reset_gate(dest: DatevDestination) -> None:
    _reset_gate_from(dest, "address_verified")


def set_target_address(db: Session, dest: DatevDestination, address: str, actor: Actor) -> None:
    address = address.strip().lower()
    if not _EMAIL_RE.fullmatch(address):
        raise GateError("Ungültige E-Mail-Adresse")
    if dest.channel == DatevChannel.upload_mail and not address.endswith("@" + UPLOAD_MAIL_DOMAIN):
        raise GateError(f"DATEV Upload Mail muss auf @{UPLOAD_MAIL_DOMAIN} enden")
    old = dest.target_address
    dest.target_address = address
    _reset_gate(dest)
    audit(db, actor, "datev.address_changed", company_id=dest.company_id, entity_type="datev_destination",
          entity_id=dest.id, details={"old": old, "new": address, "gate_reset": True})


def set_sender(
    db: Session,
    dest: DatevDestination,
    actor: Actor,
    *,
    sender_address: str,
    smtp_host: str,
    smtp_port: int,
    smtp_tls_mode: str,
    smtp_username: str,
    smtp_credential_ref: str,
) -> None:
    validate_credential_ref(smtp_credential_ref)
    if not _EMAIL_RE.fullmatch(sender_address):
        raise GateError("Ungültige Absenderadresse")
    dest.sender_address = sender_address.strip().lower()
    dest.smtp_host = smtp_host.strip()
    dest.smtp_port = smtp_port
    dest.smtp_tls_mode = smtp_tls_mode
    dest.smtp_username = smtp_username.strip()
    dest.smtp_credential_ref = smtp_credential_ref
    # Ein anderer Absender muss bei DATEV neu autorisiert werden; alles danach ebenfalls neu prüfen.
    _reset_gate_from(dest, "sender_authorized")
    audit(db, actor, "datev.sender_changed", company_id=dest.company_id, entity_type="datev_destination",
          entity_id=dest.id, details={"sender": dest.sender_address, "smtp_host": dest.smtp_host, "gate_reset_from": "sender_authorized"})


def verify_address(db: Session, dest: DatevDestination, confirmation: str, actor: Actor) -> None:
    """Adresse gilt erst als verifiziert, wenn der Admin sie exakt erneut eingibt."""
    if not dest.target_address:
        raise GateError("Keine Zieladresse hinterlegt")
    if confirmation.strip().lower() != dest.target_address:
        raise GateError("Bestätigung stimmt nicht mit der hinterlegten Adresse überein")
    dest.address_verified_at = _now()
    dest.address_verified_by = actor.user_id
    audit(db, actor, "datev.address_verified", company_id=dest.company_id, entity_type="datev_destination",
          entity_id=dest.id, details={"address": dest.target_address,
                                      "format_warnings": address_format_warnings(dest.target_address)})


def confirm_step(db: Session, dest: DatevDestination, step: str, actor: Actor, note: str | None = None) -> None:
    if step not in CONFIRMABLE_STEPS:
        raise GateError(f"Unbekannter Schritt: {step}")
    if dest.address_verified_at is None:
        raise GateError("Zuerst die Zieladresse verifizieren")
    if step == "sender_authorized" and not (dest.sender_address and dest.smtp_host):
        raise GateError("Zuerst SMTP-Absender konfigurieren")
    setattr(dest, f"{step}_at", _now())
    setattr(dest, f"{step}_by", actor.user_id)
    audit(db, actor, f"datev.gate.{step}", company_id=dest.company_id, entity_type="datev_destination",
          entity_id=dest.id, details={"note": note} if note else {})


def revoke_step(db: Session, dest: DatevDestination, step: str, actor: Actor) -> None:
    if step not in GATE_STEPS:
        raise GateError(f"Unbekannter Schritt: {step}")
    setattr(dest, f"{step}_at", None)
    setattr(dest, f"{step}_by", None)
    dest.production_released_at = None
    dest.production_released_by = None
    dest.auto_send_enabled = False
    audit(db, actor, f"datev.gate.{step}.revoked", company_id=dest.company_id, entity_type="datev_destination", entity_id=dest.id)


def missing_steps(dest: DatevDestination) -> list[str]:
    return [step for step in GATE_STEPS if getattr(dest, f"{step}_at") is None]


def release_production(db: Session, dest: DatevDestination, actor: Actor, acknowledgement: str) -> None:
    missing = missing_steps(dest)
    if missing:
        raise GateError("Offene Freigabeschritte: " + ", ".join(GATE_STEPS[m] for m in missing))
    if acknowledgement.strip() != "PRODUKTIV FREIGEBEN":
        raise GateError('Zur Bestätigung exakt "PRODUKTIV FREIGEBEN" eingeben')
    dest.production_released_at = _now()
    dest.production_released_by = actor.user_id
    audit(db, actor, "datev.production_released", company_id=dest.company_id, entity_type="datev_destination", entity_id=dest.id)


def set_auto_send(db: Session, dest: DatevDestination, enabled: bool, actor: Actor) -> None:
    if enabled and dest.production_released_at is None:
        raise GateError("Automatischer Versand erst nach Produktivfreigabe möglich")
    dest.auto_send_enabled = enabled
    audit(db, actor, "datev.auto_send_" + ("enabled" if enabled else "disabled"), company_id=dest.company_id,
          entity_type="datev_destination", entity_id=dest.id)


@dataclass
class SendPermission:
    allowed: bool
    test_mode: bool
    reasons: list[str] = field(default_factory=list)


SendMode = Literal["automatic", "manual", "test_delivery"]
# Für eine Testlieferung (vor der Produktivfreigabe) müssen diese Schritte erledigt sein;
# "Testrechnungen angekommen" kann naturgemäß erst danach bestätigt werden.
TEST_DELIVERY_STEPS = ("address_verified", "sender_authorized", "assignment_tested", "duplicate_check_tested")


def send_blockers(db: Session, dest: DatevDestination, settings: Settings, mode: SendMode) -> tuple[list[str], bool]:
    """Gründe, die einen *echten* Versand an DATEV verhindern, und ob der Testmodus aktiv ist.

    Für jeden echten Versand gelten: Umgebungsvariable, Testmodus aus, Adresse,
    SMTP-Zugang. Zusätzlich je Art:
      automatic     – alle Freigabeschritte, Produktivfreigabe, Auto-Versand an
      manual        – alle Freigabeschritte, Produktivfreigabe
      test_delivery – Schritte 1–4 (Testlieferung vor der Freigabe)
    """
    reasons: list[str] = []
    test_mode = bool(app_settings.get_setting(db, "datev.test_mode", dest.company_id))
    if test_mode:
        reasons.append("Testmodus ist aktiv")
    if not settings.datev_allow_real_send:
        reasons.append("DATEV_ALLOW_REAL_SEND ist nicht true")
    if mode == "automatic" and not dest.auto_send_enabled:
        reasons.append("Automatischer Versand ist für dieses Unternehmen deaktiviert")
    if mode in ("automatic", "manual") and dest.production_released_at is None:
        reasons.append("Keine Produktivfreigabe")
    required = GATE_STEPS if mode != "test_delivery" else TEST_DELIVERY_STEPS
    reasons += [f"Offen: {GATE_STEPS[m]}" for m in missing_steps(dest) if m in required]
    if dest.channel == DatevChannel.upload_mail:
        if not dest.target_address:
            reasons.append("Keine Zieladresse")
        if not (dest.sender_address and dest.smtp_host and dest.smtp_port and dest.smtp_username):
            reasons.append("SMTP-Absender nicht vollständig konfiguriert")
        if not dest.smtp_credential_ref or not is_secret_configured(dest.smtp_credential_ref):
            reasons.append("SMTP-Zugangsdaten nicht gesetzt")
    return reasons, test_mode


def evaluate_send_permission(db: Session, dest: DatevDestination, settings: Settings) -> SendPermission:
    """Darf für dieses Ziel jetzt *automatisch* real an DATEV gesendet werden? Alle Ebenen müssen zustimmen."""
    reasons, test_mode = send_blockers(db, dest, settings, "automatic")
    return SendPermission(allowed=not reasons, test_mode=test_mode, reasons=reasons)
