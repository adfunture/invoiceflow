"""Laufzeit-Einstellungen in ``application_settings`` mit festen Schlüsseln und Validierung."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from invoiceflow.db.models import ApplicationSetting


def _bool(v: Any) -> bool:
    if not isinstance(v, bool):
        raise ValueError("erwartet true/false")
    return v


def _int_range(lo: int, hi: int) -> Callable[[Any], int]:
    def check(v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError(f"erwartet ganze Zahl zwischen {lo} und {hi}")
        return v

    return check


def _choice(*options: str | None) -> Callable[[Any], str | None]:
    def check(v: Any) -> str | None:
        if v not in options:
            raise ValueError(f"erlaubt: {', '.join(str(o) for o in options)}")
        return v

    return check


@dataclass(frozen=True)
class SettingSpec:
    default: Any
    validate: Callable[[Any], Any]
    description: str
    admin_only: bool = True


SETTINGS: dict[str, SettingSpec] = {
    "datev.test_mode": SettingSpec(
        True, _bool, "Testmodus: Es werden keine E-Mails an DATEV gesendet (Standard: an)."
    ),
    "datev.upload_mail_zugferd_enabled": SettingSpec(
        False, _bool,
        "ZUGFeRD/Factur-X-PDFs per DATEV Upload Mail senden. Erst einschalten, wenn geprüft ist, wie der "
        "DATEV-Bestand eingebettete XML verarbeitet; bis dahin bleiben sie in der Warteschlange.",
    ),
    "datev.max_mail_size_mb": SettingSpec(
        20, _int_range(1, 50), "Maximale Größe einer Mail an DATEV Upload Mail (mit Base64-Aufschlag). Mit DATEV abgleichen."
    ),
    "datev.max_attempts": SettingSpec(5, _int_range(1, 20), "Versuche bei vorübergehenden SMTP-Fehlern."),
    "ai.enabled": SettingSpec(
        False, _bool, "KI-Fallback für unklare Dokumente. Erst nach AV-Vertrag mit dem Anbieter aktivieren."
    ),
    "ai.provider": SettingSpec(None, _choice(None, "anthropic", "openai"), "KI-Anbieter für den Fallback."),
    "ai.min_confidence_percent": SettingSpec(
        90, _int_range(50, 100), "KI-Ergebnisse darunter gehen in die manuelle Prüfung."
    ),
    "ocr.provider": SettingSpec(None, _choice(None, "tesseract", "external"), "OCR für Scan-PDFs."),
    "attachments.max_size_mb": SettingSpec(25, _int_range(1, 100), "Maximale Größe je Anhang."),
    "imap.max_message_size_mb": SettingSpec(
        50, _int_range(1, 200), "Größere Nachrichten werden nicht heruntergeladen, sondern zur Prüfung markiert."
    ),
    "retention.original_documents_years": SettingSpec(
        10,
        _int_range(8, 30),
        "Aufbewahrung steuerrelevanter Originale in Jahren (gesetzlich mind. 8 Jahre für Buchungsbelege, §147 AO). "
        "Mit dem Steuerberater abstimmen.",
    ),
    "retention.non_invoice_emails_days": SettingSpec(
        365, _int_range(30, 3650), "Aufbewahrung von E-Mails ohne Rechnungsbezug (nur Metadaten/Rohdaten)."
    ),
    "retention.auto_delete_enabled": SettingSpec(
        False, _bool, "Automatische Löschung nach Ablauf der Fristen. Standard aus – nichts wird automatisch gelöscht."
    ),
}


def get_setting(db: Session, key: str, company_id: int | None = None) -> Any:
    """Unternehmenswert, sonst globaler Wert, sonst Default."""
    spec = SETTINGS[key]
    rows = db.execute(
        select(ApplicationSetting.company_id, ApplicationSetting.value).where(
            ApplicationSetting.key == key,
            (ApplicationSetting.company_id == company_id) | ApplicationSetting.company_id.is_(None),
        )
    ).all()
    by_company = {cid: value for cid, value in rows}
    if company_id is not None and company_id in by_company:
        return by_company[company_id]
    if None in by_company:
        return by_company[None]
    return spec.default


def set_setting(db: Session, key: str, value: Any, *, company_id: int | None = None, user_id: int | None = None) -> Any:
    if key not in SETTINGS:
        raise KeyError(key)
    value = SETTINGS[key].validate(value)
    stmt = insert(ApplicationSetting).values(company_id=company_id, key=key, value=value, updated_by=user_id)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_application_settings_company_key",
        set_={"value": stmt.excluded.value, "updated_by": stmt.excluded.updated_by, "updated_at": stmt.excluded.updated_at},
    )
    db.execute(stmt)
    return value


def ensure_defaults(db: Session) -> None:
    for key, spec in SETTINGS.items():
        stmt = insert(ApplicationSetting).values(company_id=None, key=key, value=spec.default)
        db.execute(stmt.on_conflict_do_nothing(constraint="uq_application_settings_company_key"))
