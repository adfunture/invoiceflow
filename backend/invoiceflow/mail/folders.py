"""Welche IMAP-Ordner werden automatisch überwacht?

Grundsatz: **alle** Ordner eines Postfachs, inklusive Unterordnern. Ausgenommen
sind standardmäßig nur Ordner, deren Inhalt keine Eingangsrechnungen sein
sollen oder die ein Risiko darstellen:

* Gesendet   – enthält eigene Ausgangsrechnungen; diese dürfen nicht als
               Eingangsrechnung an DATEV gehen.
* Entwürfe   – unfertige eigene Nachrichten.
* Papierkorb – bewusst gelöscht.
* Spam       – häufigster Weg für gefälschte Rechnungen.

Ausgeschlossene Ordner bleiben im Dashboard sichtbar und können einzeln
eingeschaltet werden. Erkennung über SPECIAL-USE-Flags (RFC 6154) und – für
Server ohne diese Flags – über übliche deutsche/englische Namen.
"""
from __future__ import annotations

import unicodedata

from invoiceflow.mail.imap import FolderListing

_SPECIAL_USE = {
    r"\sent": "Gesendete Nachrichten (eigene Ausgangsrechnungen)",
    r"\drafts": "Entwürfe",
    r"\trash": "Papierkorb",
    r"\junk": "Spam (Risiko gefälschter Rechnungen)",
    # Sammelordner, die nur Kopien anderer Ordner zeigen (z. B. Gmail „Alle Nachrichten“)
    r"\all": "Sammelansicht aller Nachrichten (Kopien)",
    r"\flagged": "Sammelansicht markierter Nachrichten (Kopien)",
}

_NAMES = {
    "Gesendete Nachrichten (eigene Ausgangsrechnungen)": {
        "sent", "sent items", "sent messages", "sent mail", "gesendet", "gesendete objekte", "gesendete elemente",
        "gesendete nachrichten", "gesendete e-mails", "postausgang", "outbox",
    },
    "Entwürfe": {"drafts", "draft", "entwürfe", "entwurf", "entwuerfe", "vorlagen", "templates"},
    "Papierkorb": {"trash", "deleted", "deleted items", "deleted messages", "papierkorb", "gelöschte objekte",
                   "gelöschte elemente", "geloeschte objekte", "bin"},
    "Spam (Risiko gefälschter Rechnungen)": {"spam", "junk", "junk e-mail", "junk-e-mail", "junk email", "spamverdacht",
                                              "bulk mail", "unerwünscht"},
}


def _leaf(name: str, delimiter: str | None) -> str:
    leaf = name.rsplit(delimiter, 1)[-1] if delimiter else name
    return unicodedata.normalize("NFC", leaf).strip().casefold()


def exclusion_reason(folder: FolderListing) -> str | None:
    """Grund für den automatischen Ausschluss oder None (= wird überwacht)."""
    for flag in folder.flags:
        if reason := _SPECIAL_USE.get(flag.lower()):
            return reason
    # Auch Unterordner von Gesendet/Papierkorb/Spam ausschließen (z. B. "Gesendet/2026").
    parts = [p for p in (folder.name.split(folder.delimiter) if folder.delimiter else [folder.name])]
    for part in parts:
        normalized = _leaf(part, None)
        for reason, names in _NAMES.items():
            if normalized in names:
                return reason
    return None
