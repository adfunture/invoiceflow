"""Regelbasierte Klassifikation und Extraktion aus Dokumenttext (PDF/OCR).

Deutsch- und englischsprachige Belege. Die Regeln sind bewusst vorsichtig:
Lieber ``unclear``/fehlendes Feld (→ KI-Fallback oder manuelle Prüfung) als
ein falscher Wert.
"""
from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal

from invoiceflow.db.enums import ClassificationMethod, DocumentType
from invoiceflow.recognition.model import (
    ExtractedInvoice,
    normalize_iban,
    normalize_invoice_number,
    normalize_vat_id,
    parse_amount,
    parse_date,
)

HEADER_LINES = 40

# Überschriften: eine Zeile, die nur aus dem Schlüsselwort besteht, optional
# gefolgt von einer Belegnummer ("Rechnung", "Rechnung Nr. RE-1", "Gutschrift GS-5").
# Sätze ("Rechnung folgt separat") und Verweise ("Lieferschein-Nr. 123") zählen nicht.
_DATE_VALUE = r"(\d{1,2}\.\d{1,2}\.\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|\d{1,2}\.?\s+[A-Za-zäöü]+\.?\s+\d{4})"
# Optional mit Datum dahinter: "Rechnung Nr. 123 vom 01.11.2026".
_TITLE_SUFFIX = (r"(?:\s+(?:nr\.?|nummer|no\.?|number|#)?\s*[:#]?\s*[A-Z0-9][\w\-/.]*"
                 r"(?:\s+(?:vom|from|dated)\s+" + _DATE_VALUE.replace("(", "(?:", 1) + r")?)?\s*$")
_TITLE_KEYWORDS: dict[DocumentType, str] = {
    DocumentType.cancellation: r"storno(?:rechnung)?|stornierung|rechnungsstorno|cancellation\s+invoice",
    DocumentType.credit_note: r"gutschrift|rechnungsgutschrift|credit\s*note",
    DocumentType.dunning: r"(?:\d\.\s*)?(?:mahnung|zahlungserinnerung|payment\s+reminder)",
    DocumentType.offer: r"angebot|kostenvoranschlag|quotation|quote|offer",
    DocumentType.order_confirmation: r"auftragsbestätigung|auftragsbestaetigung|bestellbestätigung|order\s+confirmation",
    DocumentType.delivery_note: r"lieferschein|delivery\s+note|packing\s+slip",
    DocumentType.invoice: r"(?:schluss|abschlags|teil|sammel|honorar)?rechnung|rechnungskorrektur|korrekturrechnung|honorarnote|"
                          r"invoice|faktura|tax\s+invoice",
}
_TITLE_PATTERNS: dict[DocumentType, re.Pattern[str]] = {
    t: re.compile(r"^\s*(?:" + kw + r")\b" + _TITLE_SUFFIX, re.I) for t, kw in _TITLE_KEYWORDS.items()
}
# Mahnungen nennen die gemahnte Rechnung oft in der Überschrift ("Mahnung zu Rechnung RE-1").
_DUNNING_ANYWHERE = re.compile(r"^\s*(?:\d\.\s*)?(?:mahnung|zahlungserinnerung|payment\s+reminder)\b", re.I | re.M)
_CORRECTION = re.compile(r"\b(rechnungskorrektur|korrekturrechnung|korrigierte\s+rechnung|corrected\s+invoice)\b", re.I)
_PROFORMA = re.compile(r"\bpro\s*-?\s*forma\b", re.I)
_REVERSE_CHARGE = re.compile(
    r"reverse[\s-]*charge|steuerschuldnerschaft\s+des\s+leistungsempf|§\s*13b\s*ustg|"
    r"vat\s+(?:is\s+)?not\s+(?:applicable|charged)", re.I)


def _missing_vat_note(text: str, currency: str | None) -> str:
    """Fehlende USt ist bei Leistungen aus dem Ausland normal (Reverse Charge) – Hinweis statt Fehlerverdacht."""
    if _REVERSE_CHARGE.search(text):
        return "Keine USt ausgewiesen – Reverse Charge laut Beleg (§ 13b UStG, Steuer bucht die Buchhaltung)"
    if currency and currency != "EUR":
        return (f"Keine USt ausgewiesen – Beleg in {currency}, vermutlich Leistung aus dem Ausland "
                "(Reverse Charge, § 13b UStG)")
    return "Umsatzsteuerbetrag nicht gefunden"

_NUMBER_LABEL = re.compile(
    r"(?:rechnungs?[-\s]*(?:nummer|nr\.?)|rechnung\s+(?:nr\.?|nummer)|beleg[-\s]*(?:nummer|nr\.?)|honorarnote\s*(?:nr\.?|nummer)|"
    r"gutschrift(?:s)?[-\s]*(?:nummer|nr\.?)|storno[-\s]*(?:nummer|nr\.?)|invoice\s*(?:no\.?|number|#|nr\.?)|"
    r"credit\s*note\s*(?:no\.?|number)|document\s*(?:no\.?|number))"
    r"\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/._]{1,40})",
    re.I,
)
_REF_LABEL = re.compile(
    r"(?:zu\s+rechnung|bezug(?:nehmend)?\s+auf\s+rechnung|ursprungsrechnung|original\s+invoice|korrektur\s+zu(?:r)?\s+rechnung|storno\s+zu(?:r)?\s+rechnung)"
    r"\s*(?:nr\.?|nummer)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-/._]{1,40})",
    re.I,
)
_INVOICE_DATE = re.compile(r"\b(?:rechnungsdatum|belegdatum|gutschriftsdatum|invoice\s+date)\s*[:]?\s*" + _DATE_VALUE, re.I)
# "Rechnung Nr. 123 vom 01.11.2026": Datum hängt an der Rechnungsnummer.
_NUMBER_DATE = re.compile(_NUMBER_LABEL.pattern + r"\s+(?:vom|from|dated)\s+" + _DATE_VALUE, re.I)
_GENERIC_DATE = re.compile(r"\b(?:datum|date)\s*[:]?\s*" + _DATE_VALUE, re.I)
_SERVICE_DATE = re.compile(r"\b(?:leistungsdatum|lieferdatum|leistungszeitraum|liefer-?/?leistungsdatum|delivery\s+date|service\s+date)\s*[:]?\s*" + _DATE_VALUE
                           + r"(?:\s*(?:-|–|bis)\s*" + _DATE_VALUE + r")?", re.I)
_DUE_DATE = re.compile(r"(?:fällig(?:\s+am)?|zahlbar\s+bis(?:\s+zum)?|zahlungsziel|due\s+date|payable\s+by)\s*[:]?\s*" + _DATE_VALUE
                       + r"|(?:zahlbar|fällig|payable)[^\n]{0,50}?\b(?:bis|until|by)\s+(?:zum\s+|spätestens\s+)?" + _DATE_VALUE, re.I)
_AMOUNT = r"(-?\s?(?:\d{1,3}(?:[.,\s']\d{3})+|\d+)(?:[.,]\d{2})?\s?-?)"
_CUR = r"(?:€|euro?|usd|chf|gbp|\$)?"
# Zwischen Bezeichnung und Betrag nur Trennzeichen/Währung, keine Wörter:
# sonst würde "Rechnungsbetrag innerhalb von 7 Tagen" als Betrag 7 gelesen.
_GAP = r"[ \t:=.()]{0,15}"
_GROSS = re.compile(r"\b(?:gesamtbetrag|rechnungsbetrag|bruttobetrag|gesamtsumme|endbetrag|zahlbetrag|zu\s+zahlen(?:der\s+betrag)?|"
                    r"summe\s+brutto|brutto(?:summe)?|gesamt(?:\s+brutto|\s+eur)?|total(?:\s+amount|\s+due)?|amount\s+due|"
                    r"grand\s+total|gutschriftsbetrag|summe)(?!\s*netto)"
                    + _GAP + _CUR + _GAP + _AMOUNT + r"\s*" + _CUR, re.I)
_NET = re.compile(r"\b(?:nettobetrag|summe\s+netto|gesamt\s+netto|netto(?:summe|betrag)?|zwischensumme(?:\s+netto)?|subtotal|net\s+amount|total\s+net)"
                  + _GAP + _CUR + _GAP + _AMOUNT + r"\s*" + _CUR, re.I)
_TAX = re.compile(r"\b(?:(?:zzgl\.?|inkl\.?|plus)\s+)?(?:mwst\.?|ust\.?|umsatzsteuer|mehrwertsteuer|vat)\s*(?:\d{1,2}(?:[.,]\d+)?\s*%)?"
                  + _GAP + _CUR + _GAP + _AMOUNT + r"\s*" + _CUR, re.I)
_ANY_DATE = re.compile(r"(?<![\d.])(\d{1,2}\.\d{1,2}\.\d{4})(?![\d.])")
_CUSTOMER_VAT_CONTEXT = re.compile(
    r"\b(ihre|kunden|customer|your|buyer|käufer|empfänger|leistungsempfänger|rechnungsempfänger)", re.I
)
_VAT_ID = re.compile(r"\b(DE\s?\d{3}\s?\d{3}\s?\d{3}|ATU\d{8}|(?:BE|NL|FR|IT|ES|PL|LU|DK|SE|FI|CZ|IE|PT)\s?[A-Z0-9]{8,12})\b")
_TAX_NUMBER = re.compile(r"(?:steuer-?nr\.?|steuernummer|st\.-nr\.?)\s*[:]?\s*(\d{2,3}/\d{3}/\d{4,5}|\d{10,13})", re.I)
_IBAN = re.compile(r"\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?)\b")
_LEGAL_FORM_LINE = re.compile(
    r"([A-ZÄÖÜ0-9][\w&.,'\- ÄÖÜäöüß]{1,80}?\b(?:GmbH\s*&\s*Co\.?\s*KG|GmbH|AG|KG|OHG|UG(?:\s*\(haftungsbeschränkt\))?|"
    r"e\.\s?K\.|GbR|SE|Ltd\.?|Inc\.?|LLC|S\.A\.|S\.r\.l\.|B\.V\.))(?=$|[\s,·|•])",
)
_PAYMENT_TERMS = re.compile(r"^.*(?:zahlbar|zahlungsziel|zahlungsbedingung|payable|payment\s+terms|ohne\s+abzug|skonto).*$", re.I | re.M)
_CURRENCY = re.compile(r"(€|\bEUR\b|\bEuro\b|\bUSD\b|\bCHF\b|\bGBP\b|\$)", re.I)
_BANK_LINE = re.compile(r"^\s*(?:iban|bic|swift|blz)\b", re.I)
_BARE_VAT_LINE = re.compile(r"^\s*" + _VAT_ID.pattern + r"\s*$")
# So viele Zeilen nach dem eigenen Firmennamen gelten als Empfänger-Adressfeld.
_RECIPIENT_BLOCK_LINES = 6


def classify(text: str) -> tuple[DocumentType, Decimal, bool]:
    """→ (Dokumentart, Konfidenz, ist_Korrektur)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    scores: Counter[DocumentType] = Counter()
    for i, line in enumerate(lines):
        weight = 3 if i < HEADER_LINES else 1
        for doc_type, pattern in _TITLE_PATTERNS.items():
            if pattern.search(line):
                scores[doc_type] += weight
    if _NUMBER_LABEL.search(text):
        scores[DocumentType.invoice] += 2
    if _DUNNING_ANYWHERE.search("\n".join(lines[:HEADER_LINES])):
        scores[DocumentType.dunning] += 3
    is_correction = bool(_CORRECTION.search(text))
    if is_correction:
        scores[DocumentType.invoice] += 3

    if not scores:
        return DocumentType.unclear, Decimal("0"), is_correction
    # Gutschrift/Storno/Mahnung beziehen sich typischerweise auf eine Rechnung
    # und enthalten das Wort "Rechnung" – ihre Überschrift hat Vorrang.
    for overriding in (DocumentType.cancellation, DocumentType.credit_note, DocumentType.dunning):
        if scores[overriding] >= 3:
            return overriding, Decimal("0.900"), is_correction
    if _PROFORMA.search("\n".join(lines[:HEADER_LINES])):
        return DocumentType.other, Decimal("0.800"), False  # Proforma ist keine Buchungsrechnung
    (top, top_score), *rest = scores.most_common()
    second = rest[0][1] if rest else 0
    if top_score < 3:
        return DocumentType.unclear, Decimal("0.300"), is_correction
    if second and second * 10 >= top_score * 8:  # zu knapp → unklar
        return DocumentType.unclear, Decimal("0.400"), is_correction
    confidence = Decimal("0.950") if not second else Decimal("0.800")
    return top, confidence, is_correction


def _first_amount(pattern: re.Pattern[str], text: str, *, last: bool = False) -> Decimal | None:
    values = [parse_amount(m.group(1)) for m in pattern.finditer(text)]
    values = [v for v in values if v is not None]
    if not values:
        return None
    return values[-1] if last else values[0]


def _clean_number(value: str) -> str | None:
    value = value.strip(" .:,;")
    if not re.search(r"\d", value) or len(value) < 2:
        return None
    if parse_date(value):  # "Rechnungsnummer 01.11.2026" wäre ein Datum
        return None
    return value


def extract_fields(text: str, inv: ExtractedInvoice, own_aliases: list[str], own_vat_ids: list[str], source: str = "text") -> None:
    """Füllt leere Felder von ``inv`` aus dem Text."""
    numbers = []
    for m in _NUMBER_LABEL.finditer(text):
        n = _clean_number(m.group(1))
        if n and normalize_invoice_number(n) not in {normalize_invoice_number(x) for x in numbers}:
            numbers.append(n)
    refs = {normalize_invoice_number(_clean_number(m.group(1)) or "") for m in _REF_LABEL.finditer(text)}
    numbers = [n for n in numbers if normalize_invoice_number(n) not in refs] or numbers
    if numbers:
        inv.set("invoice_number", numbers[0], source)
        inv.other_invoice_numbers = numbers[1:]
    for m in _REF_LABEL.finditer(text):
        ref = _clean_number(m.group(1))
        if ref and normalize_invoice_number(ref) != normalize_invoice_number(inv.invoice_number):
            inv.set("referenced_invoice_number", ref, source)
            break

    if m := _INVOICE_DATE.search(text):
        inv.set("invoice_date", parse_date(m.group(1)), source)
    elif m := _NUMBER_DATE.search(text):
        inv.set("invoice_date", parse_date(m.group(2)), source)
    elif m := _GENERIC_DATE.search(text):
        inv.set("invoice_date", parse_date(m.group(1)), source)
    if m := _SERVICE_DATE.search(text):
        inv.set("service_date", parse_date(m.group(1)), source)
        if m.group(2):
            inv.set("service_period_end", parse_date(m.group(2)), source)
    if m := _DUE_DATE.search(text):
        inv.set("due_date", parse_date(m.group(1) or m.group(2)), source)
    if inv.invoice_date is None:
        _infer_header_date(text, inv, source)

    inv.set("gross_amount", _first_amount(_GROSS, text, last=True), source)
    inv.set("net_amount", _first_amount(_NET, text, last=True), source)
    inv.set("tax_amount", _first_amount(_TAX, text, last=True), source)
    if inv.document_type in (DocumentType.credit_note, DocumentType.cancellation):
        # Beträge positiv speichern; die Art steckt im Dokumenttyp.
        for name in ("net_amount", "tax_amount", "gross_amount"):
            value = getattr(inv, name)
            if value is not None and value < 0:
                setattr(inv, name, -value)

    currencies = Counter({"€": "EUR", "EURO": "EUR", "$": "USD"}.get(c.upper(), c.upper()) for c in _CURRENCY.findall(text))
    if currencies:
        inv.set("currency", currencies.most_common(1)[0][0], source)
    if inv.gross_amount is not None and inv.net_amount is not None and inv.tax_amount is None:
        inv.warnings.append(_missing_vat_note(text, inv.currency))

    own_vat = {normalize_vat_id(v) for v in own_vat_ids if v}
    recipient_lines = _recipient_block(text, own_aliases)
    for i, line in enumerate(text.splitlines()):
        if _CUSTOMER_VAT_CONTEXT.search(line) or i in recipient_lines:  # USt-ID des Empfängers
            continue
        vat = next((normalize_vat_id(m.group(1)) for m in _VAT_ID.finditer(line)), None)
        if vat and vat not in own_vat:
            inv.set("supplier_vat_id", vat, source)
            break
    if m := _TAX_NUMBER.search(text):
        inv.set("supplier_tax_number", m.group(1), source)
    for m in _IBAN.finditer(text):
        iban = normalize_iban(m.group(1))
        if iban:
            inv.set("iban", iban, source)
            break
    if m := _PAYMENT_TERMS.search(text):
        inv.set("payment_terms", m.group(0).strip()[:500], source)

    inv.set("supplier_name", _supplier_name(text, own_aliases), source)


def _infer_header_date(text: str, inv: ExtractedInvoice, source: str) -> None:
    """Rechnungsdatum steht oft in einer Spalte getrennt vom Label "Datum:".
    Gibt es im Kopfbereich genau **ein** Datum (Fälligkeit ausgenommen), ist es eindeutig."""
    header = [ln for ln in text.splitlines() if ln.strip()][:HEADER_LINES]
    dates = {
        d for ln in header if not re.search(r"fällig|zahlbar|due|payable|gültig|valid", ln, re.I)
        for m in _ANY_DATE.finditer(ln) if (d := parse_date(m.group(1)))
    }
    if len(dates) == 1 and re.search(r"\b(datum|date)\b", "\n".join(header), re.I):
        inv.set("invoice_date", dates.pop(), f"{source}_header")
        inv.warnings.append("Rechnungsdatum aus dem Kopfbereich abgeleitet (einziges Datum neben dem Label)")


def _supplier_name(text: str, own_aliases: list[str]) -> str | None:
    own = [a.lower() for a in own_aliases if a]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # Absenderzeile im Adressfeld (oben) und Fußzeile (unten) sind die typischen Orte.
    head, tail = list(range(min(12, len(lines)))), list(range(max(12, len(lines) - 15), len(lines)))
    for i in head + tail:
        line = lines[i]
        # Bankverbindung ("Volksbank AG" über "IBAN: …") ist nicht der Lieferant.
        if _BANK_LINE.search(line) or (i + 1 < len(lines) and _BANK_LINE.match(lines[i + 1])):
            continue
        for m in _LEGAL_FORM_LINE.finditer(line):
            name = re.sub(r"\s+", " ", m.group(1)).strip(" ,·|-")
            if any(alias in name.lower() or name.lower() in alias for alias in own):
                continue
            if len(name) >= 4:
                return name
    return None


def _recipient_block(text: str, own_aliases: list[str]) -> set[int]:
    """Zeilennummern im Empfänger-Adressfeld (unter dem eigenen Firmennamen), die nur eine
    USt-ID ohne Label enthalten. So drucken ausländische Lieferanten die USt-ID des Empfängers
    (Reverse Charge). Eine gelabelte eigene USt-ID ("USt-IdNr.: …", Fußzeile) bleibt verdächtig."""
    own = [a.lower() for a in own_aliases if a and len(a) >= 4]
    lines = text.splitlines()
    block: set[int] = set()
    for i, line in enumerate(lines):
        if any(alias in line.lower() for alias in own):
            block.update(j for j in range(i + 1, min(len(lines), i + 1 + _RECIPIENT_BLOCK_LINES))
                         if _BARE_VAT_LINE.match(lines[j]))
    return block


def own_vat_as_sender(text: str, own_vat_ids: list[str], own_aliases: list[str] | None = None) -> str | None:
    """Eigene USt-ID außerhalb eines Kunden-/Empfängerkontexts → vermutlich eigene Ausgangsrechnung."""
    own = {normalize_vat_id(v) for v in own_vat_ids if v}
    if not own:
        return None
    recipient_lines = _recipient_block(text, own_aliases or [])
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _CUSTOMER_VAT_CONTEXT.search(line) or i in recipient_lines:
            continue
        for m in _VAT_ID.finditer(line):
            if (vat := normalize_vat_id(m.group(1))) in own and not _label_above_is_customer(lines, i, m.start()):
                return vat
    return None


_CELL_SPLIT = re.compile(r"\s{2,}")


def _label_above_is_customer(lines: list[str], i: int, pos: int) -> bool:
    """Tabellen mit Bezeichnung über dem Wert ("Ihre USt-ID-Nr.  Unsere USt-ID" / "DE…  DE…"):
    Die Bezeichnung derselben Spalte in der Zeile darüber entscheidet, wessen USt-ID es ist."""
    above = next((lines[j] for j in range(i - 1, max(-1, i - 3), -1) if lines[j].strip()), "")
    if not above:
        return False
    cells, labels = _CELL_SPLIT.split(lines[i].strip()), _CELL_SPLIT.split(above.strip())
    if len(cells) == len(labels) and len(cells) > 1:
        before = lines[i][:pos].strip()
        column = len(_CELL_SPLIT.split(before)) if before else 0
        return bool(_CUSTOMER_VAT_CONTEXT.search(labels[min(column, len(labels) - 1)]))
    # Spalten nicht eindeutig zuordenbar: nur wenn die Zeile darüber eine reine Kunden-Bezeichnung ist.
    return bool(_CUSTOMER_VAT_CONTEXT.search(above)) and not _OWN_VAT_LABEL.search(above)


_OWN_VAT_LABEL = re.compile(r"\b(unsere|our|seller|verkäufer)\b", re.I)
