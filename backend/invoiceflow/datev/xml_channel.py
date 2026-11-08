"""Schnittstelle für reine XML-Rechnungen (XRechnung) an DATEV.

DATEV Upload Mail ist für XML-Rechnungen nicht vorgesehen. Bis eine
dokumentierte DATEV-Schnittstelle angebunden ist (z. B. über die
DATEV-Marktplatz-APIs mit eigener Zulassung), bleiben XML-Rechnungen
unverändert im Speicher und in der Warteschlange
(``documents.status = awaiting_datev_xml_integration``).

Bewusst **keine** erfundenen Endpunkte: Eine Implementierung von
``DatevXmlChannel`` wird erst geschrieben, wenn Zugang und Dokumentation
vorliegen.
"""
from __future__ import annotations

from typing import Protocol

from invoiceflow.db.models import Document


class XmlChannelUnavailable(RuntimeError):
    pass


class DatevXmlChannel(Protocol):
    name: str

    def submit(self, document: Document, xml: bytes) -> str:
        """Übergibt die XML-Rechnung; liefert eine Referenz des Zielsystems."""


class UnconfiguredXmlChannel:
    name = "unconfigured"

    def submit(self, document: Document, xml: bytes) -> str:
        raise XmlChannelUnavailable(
            "Keine DATEV-Schnittstelle für XML-Rechnungen konfiguriert. Die Datei bleibt unverändert in der "
            "XML-Warteschlange und kann über den Original-Download manuell in DATEV hochgeladen werden."
        )


def get_xml_channel() -> DatevXmlChannel:
    return UnconfiguredXmlChannel()
