"""Prüft, ob ein Beleg an das Unternehmen des Postfachs adressiert ist.

Die Zuordnung ändert sich nie automatisch. Ist ein Beleg erkennbar an das
andere Unternehmen adressiert (und nicht an das eigene), wird er zur
manuellen Prüfung zurückgehalten.
"""
from __future__ import annotations

from dataclasses import dataclass

RECIPIENT_ZONE_LINES = 40


@dataclass(frozen=True)
class CompanyNames:
    company_id: int
    short_name: str
    aliases: list[str]


def _hits(text: str, aliases: list[str]) -> bool:
    folded = " ".join(text.split()).casefold()
    return any(a.strip() and a.casefold() in folded for a in aliases)


def recipient_mismatch(own: CompanyNames, others: list[CompanyNames], text: str, recipient_name: str | None) -> str | None:
    """Grund für die Zurückhaltung oder None."""
    zone = "\n".join([ln for ln in text.splitlines() if ln.strip()][:RECIPIENT_ZONE_LINES])
    if recipient_name:
        if _hits(recipient_name, own.aliases):
            return None
        for other in others:
            if _hits(recipient_name, other.aliases):
                return (f"Rechnungsempfänger ist {other.short_name} ({recipient_name}), "
                        f"Postfach gehört zu {own.short_name}")
    if _hits(text, own.aliases):
        return None  # eigenes Unternehmen genannt (anderes kann z. B. Lieferant sein)
    for other in others:
        if _hits(zone, other.aliases):
            return f"Beleg ist an {other.short_name} adressiert, Postfach gehört zu {own.short_name}"
    return None
