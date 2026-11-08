"""OCR für Scan-PDFs und Bild-Anhänge.

Konfiguration über die Einstellung ``ocr.provider``:

* ``tesseract`` – lokale Texterkennung (Tesseract + PyMuPDF zum Rendern).
  Im Docker-Image installiert (Pakete ``tesseract-ocr``, ``tesseract-ocr-deu``).
  Daten verlassen den Server nicht.
* ``external`` – Schnittstelle für einen externen Dienst. Noch nicht
  angebunden: Anbieter und Auftragsverarbeitungsvertrag fehlen.
* nicht gesetzt – Scans gehen in die manuelle Prüfung.
"""
from __future__ import annotations

import io
from typing import Protocol

MAX_OCR_PAGES = 20
RENDER_DPI = 300


class OcrUnavailable(RuntimeError):
    pass


class OcrProvider(Protocol):
    name: str

    def pdf_to_text(self, data: bytes) -> str: ...
    def image_to_text(self, data: bytes) -> str: ...


class TesseractOcr:
    name = "tesseract"

    def __init__(self, languages: str = "deu+eng") -> None:
        try:
            import fitz  # PyMuPDF
            import pytesseract
            from PIL import Image
        except ImportError as exc:
            raise OcrUnavailable("OCR-Pakete fehlen (pip install 'invoiceflow[ocr]')") from exc
        try:
            pytesseract.get_tesseract_version()
        except Exception as exc:  # noqa: BLE001
            raise OcrUnavailable("Tesseract ist nicht installiert") from exc
        self._fitz, self._tess, self._image = fitz, pytesseract, Image
        self.languages = languages

    def pdf_to_text(self, data: bytes) -> str:
        pages: list[str] = []
        with self._fitz.open(stream=data, filetype="pdf") as doc:
            for index, page in enumerate(doc):
                if index >= MAX_OCR_PAGES:
                    break
                pix = page.get_pixmap(dpi=RENDER_DPI)
                image = self._image.open(io.BytesIO(pix.tobytes("png")))
                pages.append(self._tess.image_to_string(image, lang=self.languages))
        return "\n".join(pages)

    def image_to_text(self, data: bytes) -> str:
        image = self._image.open(io.BytesIO(data))
        image.load()  # erzwingt Dekodierung; kaputte Bilder werfen hier
        return self._tess.image_to_string(image, lang=self.languages)


def get_ocr(provider: str | None) -> OcrProvider:
    if provider == "tesseract":
        return TesseractOcr()
    if provider == "external":
        raise OcrUnavailable("Externer OCR-Dienst ist noch nicht angebunden (Anbieter/AVV offen)")
    raise OcrUnavailable("OCR ist nicht konfiguriert (Einstellung ocr.provider)")
