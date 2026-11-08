"""PDF-Analyse: Text, Seitenzahl, eingebettete E-Rechnungs-XML (ZUGFeRD/Factur-X).

Das Original wird nur gelesen. Eingebettete XML wird als eigene Datei
abgelegt; das PDF bleibt Byte für Byte unverändert.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

from pypdf import PdfReader
from pypdf.errors import PdfReadError

log = logging.getLogger("invoiceflow.pdf")
logging.getLogger("pypdf").setLevel(logging.ERROR)  # pypdf meldet harmlose Strukturwarnungen sehr gesprächig

# Bekannte Dateinamen der eingebetteten Rechnungs-XML (ZUGFeRD 1/2, Factur-X, XRechnung-Hybrid).
EINVOICE_NAMES = ("factur-x.xml", "zugferd-invoice.xml", "zugferd_invoice.xml", "xrechnung.xml", "order-x.xml")
MAX_PAGES_FOR_TEXT = 50
# Unter so vielen Zeichen je Seite gilt ein PDF als Scan (Bild ohne Textebene).
SCAN_CHARS_PER_PAGE = 40


class PdfError(ValueError):
    pass


@dataclass
class PdfAnalysis:
    page_count: int
    pages_text: list[str]
    embedded_xml: bytes | None = None
    embedded_xml_name: str | None = None
    embedded_files: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.pages_text)

    @property
    def is_scan(self) -> bool:
        if self.page_count == 0:
            return True
        chars = sum(len(t.strip()) for t in self.pages_text)
        return chars / max(1, len(self.pages_text)) < SCAN_CHARS_PER_PAGE


def analyze_pdf(data: bytes) -> PdfAnalysis:
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            # Leeres Benutzerpasswort ist bei "geschützten" Rechnungen häufig.
            if not reader.decrypt(""):
                raise PdfError("PDF ist passwortgeschützt")
        pages = reader.pages
        count = len(pages)
        texts = []
        for page in list(pages)[:MAX_PAGES_FOR_TEXT]:
            try:
                texts.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 – einzelne kaputte Seite
                texts.append("")
        analysis = PdfAnalysis(page_count=count, pages_text=texts)
        try:
            attachments = reader.attachments  # {name: [bytes, ...]}
        except Exception:  # noqa: BLE001
            attachments = {}
        analysis.embedded_files = sorted(attachments)
        for name in attachments:
            if name.lower() in EINVOICE_NAMES or (name.lower().endswith(".xml") and len(attachments) == 1):
                contents = attachments[name]
                if contents:
                    analysis.embedded_xml, analysis.embedded_xml_name = contents[0], name
                    break
        return analysis
    except PdfError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise PdfError(f"PDF nicht lesbar: {type(exc).__name__}") from exc
