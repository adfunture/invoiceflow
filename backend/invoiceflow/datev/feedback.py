"""Antworten von DATEV Upload Mail ("AW: Beleg: …" von noreply@uploadmail.datev.de).

DATEV quittiert jeden eingereichten Beleg per Mail an den Absender. Diese Mails sind
keine Rechnungspost: Sie werden nicht importiert, sondern als Rückmeldung zur
Übertragung verbucht. Die Zuordnung läuft über In-Reply-To/References, die auf die
Message-ID unserer Versandmail zeigen (``…invoiceflow-<Übertragungs-ID>@…``).
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from invoiceflow.datev.operations import record_feedback
from invoiceflow.db.enums import TransmissionStatus
from invoiceflow.db.models import DatevTransmission
from invoiceflow.mail.mime import ParsedMessage
from invoiceflow.services.audit import Actor

DATEV_REPLY_DOMAIN = "uploadmail.datev.de"
_TRANSMISSION_REF = re.compile(r"invoiceflow-(\d+)@", re.I)
# Nur im Kopf der Nachricht suchen: Fußzeilen enthalten oft allgemeine Hinweise ("Bei Fehlern …").
_PROBLEM = re.compile(
    r"fehlgeschlagen|nicht\s+(?:verarbeitet|übertragen|zugestellt|angenommen|erkannt)|abgelehnt|"
    r"konnte\s+nicht|ungültig|zurückgewiesen|fehlerhaft",
    re.I,
)
PROBLEM_SCAN_CHARS = 600


def is_datev_reply(parsed: ParsedMessage) -> bool:
    return bool(parsed.from_address and parsed.from_address.lower().endswith("@" + DATEV_REPLY_DOMAIN))


def apply_datev_reply(db: Session, parsed: ParsedMessage, company_id: int, actor: Actor) -> list[int]:
    """Verbucht die Antwort bei der zugehörigen Übertragung; liefert deren IDs.

    Ohne erkennbares Problem → bestätigt. Mit Problemhinweis → manuelle Prüfung
    (erscheint auf der Seite DATEV-Versand, nicht als E-Mail)."""
    ids = {int(m.group(1)) for ref in parsed.reply_references for m in _TRANSMISSION_REF.finditer(ref)}
    if not ids:
        return []
    head = f"{parsed.subject or ''}\n{parsed.text_body[:PROBLEM_SCAN_CHARS]}"
    problem = _PROBLEM.search(head)
    handled: list[int] = []
    for t in db.scalars(select(DatevTransmission).where(
            DatevTransmission.id.in_(ids), DatevTransmission.company_id == company_id,
            DatevTransmission.test_mode.is_(False), DatevTransmission.status == TransmissionStatus.sent_unconfirmed)):
        if problem:
            snippet = re.sub(r"\s+", " ", head[max(0, problem.start() - 80):problem.end() + 120]).strip()
            record_feedback(db, t, actor, "rejected", f"DATEV meldet ein Problem: {snippet}", source="datev_mail")
        else:
            record_feedback(db, t, actor, "confirmed", f"Eingangsbestätigung von DATEV: {parsed.subject or ''}"[:500],
                            source="datev_mail")
        handled.append(t.id)
    return handled
