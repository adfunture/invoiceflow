"""Ergebnis der Rechnungserkennung und Normalisierung der Werte.

Grundsatz: Es wird nichts erfunden. Felder, die nicht eindeutig im Dokument
stehen, bleiben ``None``. Jedes Feld merkt sich seine Quelle (xml, text,
ocr, ai) in ``field_sources``.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field, fields
from datetime import date
from decimal import Decimal, InvalidOperation

from invoiceflow.db.enums import ClassificationMethod, DocumentType

REQUIRED_FIELDS = ("invoice_number", "invoice_date", "gross_amount", "currency", "supplier")
INVOICE_LIKE = {DocumentType.invoice, DocumentType.credit_note, DocumentType.cancellation}

_MONTHS = {
    "januar": 1, "jan": 1, "january": 1, "februar": 2, "feb": 2, "february": 2, "märz": 3, "maerz": 3, "mär": 3,
    "march": 3, "mar": 3, "april": 4, "apr": 4, "mai": 5, "may": 5, "juni": 6, "jun": 6, "june": 6, "juli": 7,
    "jul": 7, "july": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "oktober": 10, "okt": 10,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "dezember": 12, "dez": 12, "december": 12, "dec": 12,
}
_LEGAL_FORMS = re.compile(
    r"\b(gmbh\s*&\s*co\.?\s*kg|gmbh|ag|kg|ohg|ug(\s*\(haftungsbeschränkt\))?|e\.\s?k\.|gbr|se|ltd\.?|inc\.?|llc|"
    r"s\.a\.|s\.r\.l\.|b\.v\.|sarl|co\.?)\b",
    re.I,
)


@dataclass
class ExtractedInvoice:
    document_type: DocumentType = DocumentType.unclear
    is_correction: bool = False
    invoice_number: str | None = None
    invoice_date: date | None = None
    service_date: date | None = None
    service_period_end: date | None = None
    supplier_name: str | None = None
    supplier_address: str | None = None
    supplier_vat_id: str | None = None
    supplier_tax_number: str | None = None
    net_amount: Decimal | None = None
    tax_amount: Decimal | None = None
    gross_amount: Decimal | None = None
    currency: str | None = None
    recipient_name: str | None = None
    recipient_text: str | None = None
    iban: str | None = None
    payment_terms: str | None = None
    due_date: date | None = None
    referenced_invoice_number: str | None = None
    method: ClassificationMethod = ClassificationMethod.rule
    confidence: Decimal | None = None
    field_sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    other_invoice_numbers: list[str] = field(default_factory=list)  # Hinweis auf mehrere Rechnungen

    VALUE_FIELDS = (
        "invoice_number", "invoice_date", "service_date", "service_period_end", "supplier_name", "supplier_address",
        "supplier_vat_id", "supplier_tax_number", "net_amount", "tax_amount", "gross_amount", "currency",
        "recipient_name", "recipient_text", "iban", "payment_terms", "due_date", "referenced_invoice_number",
    )

    def set(self, name: str, value, source: str) -> None:
        """Setzt ein Feld nur, wenn es noch leer ist (erste, verlässlichste Quelle gewinnt)."""
        if value is None or value == "" or getattr(self, name) is not None:
            return
        setattr(self, name, value)
        self.field_sources[name] = source

    def missing_required(self) -> list[str]:
        missing = []
        for name in REQUIRED_FIELDS:
            if name == "supplier":
                if not (self.supplier_name or self.supplier_vat_id):
                    missing.append("supplier")
            elif getattr(self, name) is None:
                missing.append(name)
        return missing

    def amounts_consistent(self) -> bool | None:
        if None in (self.net_amount, self.tax_amount, self.gross_amount):
            return None
        return abs(self.net_amount + self.tax_amount - self.gross_amount) <= Decimal("0.02")

    def as_jsonable(self) -> dict:
        out = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, (Decimal, date)):
                value = str(value)
            elif hasattr(value, "value"):
                value = value.value
            out[f.name] = value
        return out


# --- Normalisierung -------------------------------------------------------------


def parse_amount(text: str | None) -> Decimal | None:
    """'1.234,56' / '1,234.56' / '1234,56 €' / '-12,00' / '12,00-' → Decimal."""
    if not text:
        return None
    s = text.strip().replace(" ", " ").replace("€", "").replace("EUR", "").strip()
    negative = s.startswith("-") or s.endswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("-() ").replace(" ", "").replace("'", "")
    if not re.fullmatch(r"\d{1,3}([.,]\d{3})*([.,]\d{1,2})?|\d+([.,]\d{1,2})?", s):
        return None
    last_sep = max(s.rfind(","), s.rfind("."))
    if last_sep != -1 and len(s) - last_sep - 1 in (1, 2):
        integer, decimals = s[:last_sep], s[last_sep + 1:]
    else:
        integer, decimals = s, "0"
    integer = integer.replace(".", "").replace(",", "")
    try:
        value = Decimal(f"{integer}.{decimals}").quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    return -value if negative else value


def parse_date(text: str | None) -> date | None:
    if not text:
        return None
    s = text.strip().lower()
    try:
        if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s):  # CII-Format 102
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})", s):
            year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            return date(year, int(m[2]), int(m[1]))
        if m := re.fullmatch(r"(\d{1,2})\.?\s+([a-zäöü]+)\.?\s+(\d{4})", s):
            month = _MONTHS.get(m[2])
            return date(int(m[3]), month, int(m[1])) if month else None
    except ValueError:
        return None
    return None


def normalize_invoice_number(number: str | None) -> str | None:
    if not number:
        return None
    n = unicodedata.normalize("NFKC", number).upper()
    n = re.sub(r"[\s]", "", n)
    n = re.sub(r"^(NR\.?|NO\.?|#)", "", n)
    return n.strip(".:#") or None


def normalize_vat_id(value: str | None) -> str | None:
    if not value:
        return None
    v = re.sub(r"[\s.\-]", "", value.upper())
    return v if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{8,12}", v) else None


def iban_valid(iban: str) -> bool:
    s = re.sub(r"\s", "", iban.upper())
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    rearranged = s[4:] + s[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def normalize_iban(value: str | None) -> str | None:
    if not value:
        return None
    s = re.sub(r"\s", "", value.upper())
    return s if iban_valid(s) else None


def normalize_company_name(name: str | None) -> str | None:
    if not name:
        return None
    n = unicodedata.normalize("NFKC", name).lower()
    n = _LEGAL_FORMS.sub(" ", n)
    n = re.sub(r"[^a-z0-9äöüß]+", " ", n).strip()
    return n or None


def supplier_key(inv: ExtractedInvoice) -> str | None:
    if inv.supplier_vat_id:
        return f"vat:{inv.supplier_vat_id}"
    name = normalize_company_name(inv.supplier_name)
    return f"name:{name}" if name else None


def identity_hash(company_id: int, inv: ExtractedInvoice) -> str | None:
    """Rechnungsidentität (Ebene 2). Nur bei vollständigen Kerndaten."""
    key = supplier_key(inv)
    number = normalize_invoice_number(inv.invoice_number)
    if not (key and number and inv.invoice_date and inv.gross_amount is not None and inv.currency):
        return None
    raw = "|".join([
        str(company_id), key, number, inv.invoice_date.isoformat(), f"{inv.gross_amount:.2f}", inv.currency,
        inv.document_type.value,
    ])
    return hashlib.sha256(raw.encode()).hexdigest()
