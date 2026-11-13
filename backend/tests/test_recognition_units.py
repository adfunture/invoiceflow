from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import pytest

from invoiceflow.db.enums import ClassificationMethod, DocumentType
from invoiceflow.recognition import ai as ai_mod
from invoiceflow.recognition.einvoice import EInvoiceError, detect_format, parse_einvoice
from invoiceflow.recognition.model import (
    ExtractedInvoice,
    identity_hash,
    iban_valid,
    normalize_invoice_number,
    parse_amount,
    parse_date,
)
from invoiceflow.recognition.pdf import PdfError, analyze_pdf
from invoiceflow.recognition.recipient import CompanyNames, recipient_mismatch
from invoiceflow.recognition.text_rules import classify, extract_fields, non_booking_hint, own_vat_as_sender

from .samples import cii_xml, invoice_lines, make_pdf, ubl_xml, with_embedded_xml

ADF = ["Adfunture Outdoor GmbH", "Adfunture"]


@pytest.mark.parametrize("raw,expected", [
    ("1.234,56", "1234.56"), ("1234,56 €", "1234.56"), ("1,234.56", "1234.56"), ("119,00", "119.00"),
    ("-12,00", "-12.00"), ("12,00-", "-12.00"), ("1.000", "1000.00"), ("EUR 5", "5.00"), ("abc", None), ("1,2,3", None),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == (Decimal(expected) if expected else None)


@pytest.mark.parametrize("raw,expected", [
    ("09.11.2026", date(2026, 11, 9)), ("9.11.26", date(2026, 11, 9)), ("2026-11-09", date(2026, 11, 9)),
    ("20261109", date(2026, 11, 9)), ("9. November 2026", date(2026, 11, 9)), ("31.02.2026", None), ("morgen", None),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_iban_checksum():
    assert iban_valid("DE89 3704 0044 0532 0130 00")
    assert not iban_valid("DE89 3704 0044 0532 0130 01")


def test_normalize_invoice_number():
    assert normalize_invoice_number(" Nr. re-2026 1001 ") == "RE-20261001"


def _classify(lines):
    return classify("\n".join(lines))[0]


def test_1_normal_invoice_text():
    inv = ExtractedInvoice()
    text = "\n".join(invoice_lines())
    inv.document_type, inv.confidence, _ = classify(text)
    extract_fields(text, inv, ADF, [])
    assert inv.document_type == DocumentType.invoice
    assert (inv.invoice_number, inv.invoice_date, inv.gross_amount, inv.net_amount, inv.tax_amount, inv.currency) == (
        "RE-2026-1001", date(2026, 11, 9), Decimal("1190.00"), Decimal("1000.00"), Decimal("190.00"), "EUR")
    assert inv.supplier_name == "Müller Outdoor Handel GmbH"
    assert inv.supplier_vat_id == "DE123456789"
    assert inv.iban == "DE89370400440532013000"
    assert inv.service_date == date(2026, 11, 5)
    assert inv.missing_required() == [] and inv.amounts_consistent() is True


def test_6_offer_mentioning_invoice_is_offer():
    lines = ["Zelthaus GmbH", "Adfunture Outdoor GmbH", "Angebot", "Angebotsnummer: AN-77", "Datum: 09.11.2026",
             "Die Rechnung erfolgt nach Lieferung.", "Gesamtbetrag 500,00 EUR", "Gültig bis 30.11.2026"]
    assert _classify(lines) == DocumentType.offer


def test_7_credit_note_referencing_invoice():
    lines = invoice_lines("GS-55", title="Gutschrift", extra=["Gutschrift zu Rechnung RE-2026-1001"])
    assert _classify(lines) == DocumentType.credit_note


def test_cancellation_and_dunning_and_delivery_note():
    assert _classify(invoice_lines("ST-1", title="Stornorechnung")) == DocumentType.cancellation
    assert _classify(["Firma GmbH", "Mahnung", "Rechnung Nr. RE-1 vom 01.10.2026 ist offen", "Betrag 10,00 EUR"]) == DocumentType.dunning
    assert _classify(["Firma GmbH", "Lieferschein", "Lieferschein-Nr. LS-1", "Rechnung folgt separat"]) == DocumentType.delivery_note
    assert _classify(["Firma GmbH", "Auftragsbestätigung", "Ihre Bestellung vom 01.11.2026"]) == DocumentType.order_confirmation


def test_invoice_with_delivery_note_reference_stays_invoice():
    assert _classify(invoice_lines(extra=["Lieferschein-Nr. LS-4711", "Auftragsbestätigung Nr. AB-1"])) == DocumentType.invoice


def test_correction_flag():
    _, _, correction = classify("\n".join(invoice_lines(title="Rechnungskorrektur")))
    assert correction is True


def test_own_company_not_taken_as_supplier():
    lines = ["Adfunture Outdoor GmbH", "Rechnung", "Rechnungsnummer: X-1", "Gesamtbetrag 10,00 EUR"]
    inv = ExtractedInvoice()
    extract_fields("\n".join(lines), inv, ADF, [])
    assert inv.supplier_name is None


def test_multiple_invoice_numbers_are_flagged():
    inv = ExtractedInvoice()
    extract_fields("\n".join(invoice_lines("RE-1") + ["Rechnungsnummer: RE-2"]), inv, ADF, [])
    assert inv.invoice_number == "RE-1" and inv.other_invoice_numbers == ["RE-2"]


def test_identity_hash_depends_on_type_and_amount():
    inv = ExtractedInvoice(document_type=DocumentType.invoice, invoice_number="RE-1", invoice_date=date(2026, 11, 9),
                           supplier_vat_id="DE123456789", gross_amount=Decimal("10.00"), currency="EUR")
    h1 = identity_hash(1, inv)
    inv.document_type = DocumentType.credit_note
    assert identity_hash(1, inv) != h1
    inv.document_type = DocumentType.invoice
    assert identity_hash(2, inv) != h1, "je Unternehmen getrennt"
    inv.gross_amount = Decimal("11.00")
    assert identity_hash(1, inv) != h1


# --- PDF / XML ----------------------------------------------------------------------


def test_pdf_scan_and_corrupt_detection():
    assert analyze_pdf(make_pdf([[]])).is_scan
    assert not analyze_pdf(make_pdf(invoice_lines())).is_scan
    with pytest.raises(PdfError):
        analyze_pdf(b"%PDF-1.7\nkaputt\n%%EOF")


def test_8_zugferd_embedded_xml_found():
    pdf = with_embedded_xml(make_pdf(invoice_lines("ZF-2026-77")), cii_xml())
    analysis = analyze_pdf(pdf)
    assert analysis.embedded_xml_name == "factur-x.xml"
    assert parse_einvoice(analysis.embedded_xml).invoice_number == "ZF-2026-77"


def test_cii_and_ubl_parsing():
    cii = parse_einvoice(cii_xml())
    assert cii.method == ClassificationMethod.xml
    assert (cii.document_type, cii.gross_amount, cii.supplier_vat_id, cii.recipient_name) == (
        DocumentType.invoice, Decimal("595.00"), "DE987654321", "Adfunture Outdoor GmbH")
    assert parse_einvoice(cii_xml(type_code="381")).document_type == DocumentType.credit_note
    ubl = parse_einvoice(ubl_xml())
    assert (ubl.invoice_number, ubl.net_amount, ubl.tax_amount) == ("XR-500", Decimal("200.00"), Decimal("38.00"))
    assert detect_format(ubl_xml()) == "ubl" and detect_format(b"<foo/>") is None


@pytest.mark.parametrize("payload", [
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>',
    b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><l>&b;</l>',
    b"<kaputt",
])
def test_unsafe_or_broken_xml_rejected(payload):
    with pytest.raises(EInvoiceError):
        parse_einvoice(payload)


# --- Empfänger ----------------------------------------------------------------------

OWN = CompanyNames(2, "Adfunture", ["Adfunture Outdoor GmbH", "Adfunture"])
SBC = CompanyNames(1, "SBC symbio", ["SBC symbio GmbH & Co. KG", "SBC symbio"])


def test_14_recipient_other_company_is_flagged():
    text = "\n".join(invoice_lines(recipient="SBC symbio GmbH & Co. KG"))
    assert "SBC symbio" in recipient_mismatch(OWN, [SBC], text, None)


def test_recipient_own_company_ok_even_if_other_is_supplier():
    text = "\n".join(invoice_lines(supplier="SBC symbio GmbH & Co. KG", recipient="Adfunture Outdoor GmbH"))
    assert recipient_mismatch(OWN, [SBC], text, None) is None


def test_recipient_from_xml():
    assert recipient_mismatch(OWN, [SBC], "", "SBC symbio GmbH & Co. KG")
    assert recipient_mismatch(OWN, [SBC], "", "Adfunture Outdoor GmbH") is None


# --- KI-Abgleich ----------------------------------------------------------------------


def test_18_ai_values_not_in_document_are_discarded():
    text = "\n".join(invoice_lines())
    inv = ExtractedInvoice(document_type=DocumentType.unclear)
    fields = ai_mod.AiInvoiceFields(
        document_type="invoice", confidence=0.99, invoice_number="ERFUNDEN-999", invoice_date="2026-11-09",
        gross_amount="1190.00", currency="EUR", supplier_name="Müller Outdoor Handel GmbH",
        supplier_vat_id="DE000000000", iban="DE89370400440532013000", net_amount="999.99",
    )
    result = ai_mod.apply_ai(fields, text, inv, 0.9)
    assert inv.invoice_number is None and inv.supplier_vat_id is None and inv.net_amount is None
    assert {"invoice_number", "supplier_vat_id", "net_amount"} <= set(result.rejected)
    assert inv.gross_amount == Decimal("1190.00") and inv.invoice_date == date(2026, 11, 9)
    assert inv.field_sources["gross_amount"] == "ai"
    assert inv.document_type == DocumentType.invoice and inv.method == ClassificationMethod.ai


def test_ai_schema_has_no_company_or_destination():
    props = set(ai_mod.OUTPUT_SCHEMA["properties"])
    assert not props & {"company", "company_id", "datev_address", "target_address", "status", "mailbox"}
    with pytest.raises(Exception):
        ai_mod.AiInvoiceFields.model_validate({"document_type": "invoice", "confidence": 1, "company": "SBC"})


def test_ai_low_confidence_does_not_set_type():
    inv = ExtractedInvoice(document_type=DocumentType.unclear)
    ai_mod.apply_ai(ai_mod.AiInvoiceFields(document_type="invoice", confidence=0.5), "Text", inv, 0.9)
    assert inv.document_type == DocumentType.unclear


def test_column_layout_like_real_invoice():
    """Layout eines realen Belegs (Werte erfunden): Datum getrennt vom Label, 'Gesamt' als Brutto,
    Zahl im Fließtext nach 'Rechnungsbetrag', Kunden-USt-ID vor der des Lieferanten."""
    lines = [
        "Beispiel Lieferant GmbH · Hafenstr. 1 · 20457 Hamburg", "Adfunture Outdoor GmbH", "Datum: ",
        "Kunden-Nr.: ", "Lieferscheindatum: ", "14.11.2026", "14.11.2026", "Rechnung 2026-000123",
        "Ihre USt-ID: DE999999999", "Pos Artikel Menge Gesamt", "1 Zelt 10 1.000,00",
        "Gesamt netto 1.000,00 EUR", "zzgl. MwSt. 19.00 % 190,00 EUR", "Gesamt 1.190,00 EUR",
        "Bitte überweisen Sie den Rechnungsbetrag innerhalb von 7 Tagen unter Angabe der Rechnungsnummer 2026-000123",
        "Rechnung zahlbar innerhalb von 7 Tagen bis zum 21.11.2026.", "Beispiel Lieferant GmbH USt-IdNr. DE123456789",
    ]
    text = "\n".join(lines)
    inv = ExtractedInvoice()
    inv.document_type, inv.confidence, _ = classify(text)
    extract_fields(text, inv, ADF, [])
    assert inv.document_type == DocumentType.invoice
    assert inv.gross_amount == Decimal("1190.00") and inv.amounts_consistent() is True
    assert inv.invoice_date == date(2026, 11, 14) and inv.field_sources["invoice_date"] == "text_header"
    assert inv.due_date == date(2026, 11, 21)
    assert inv.supplier_vat_id == "DE123456789", "Kunden-USt-ID ignoriert"
    assert inv.invoice_number == "2026-000123"


def test_header_date_not_inferred_when_ambiguous():
    lines = ["Lieferant GmbH", "Datum:", "Kunden-Nr.: 5", "01.11.2026", "Lieferdatum 03.11.2026", "Rechnung",
             "Rechnungsnummer: R-1"]
    inv = ExtractedInvoice()
    extract_fields("\n".join(lines), inv, ADF, [])
    assert inv.invoice_date is None


def test_ocr_not_configured_and_external_not_connected():
    from invoiceflow.recognition.ocr import OcrUnavailable, get_ocr

    for provider in (None, "external"):
        with pytest.raises(OcrUnavailable):
            get_ocr(provider)


# Honorarnote eines italienischen Freiberuflers (Südtirol): Labels und Werte stehen in
# getrennten Spalten, Datum im Format TT/MM/JJJJ, Betrag mit "Euro", Reverse Charge.
SBC_ALIASES = ["SBC symbio GmbH & Co. KG", "SBC symbio"]
_HONORARNOTE = [
    (300, 760, "SBC SYMBIO GMBH & CO. KG"), (300, 746, "GARTENSTR. 25"), (300, 732, "D-88131 BODOLZ"),
    (300, 718, "DE815512267"),
    (50, 680, "HONORARNOTE Nr."), (50, 340, "Gesamtbetrag"), (50, 300, "IBAN: IT60 X054 2811 1010 0000 0123 456"),
    (50, 314, "Muster Volksbank AG"),
    (150, 680, "956"), (200, 680, "vom"), (240, 680, "13/04/2026"),
    (300, 600, "Abschlussrechnung für das Jahr 2025"), (480, 600, "643,20"),
    (300, 586, "MwSt.-Abrechnung im 'reverse-charge' Verfahren"),
    (380, 340, "Euro"), (480, 340, "670,93"),
    (50, 80, "Muster.Partner GmbH Freiberuflergesellschaft"),
    (50, 66, "Eintragungs-, Steuer- und MwSt.-Nummer IT 01234567890"),
]


def test_layout_keeps_label_and_value_on_one_line():
    text = analyze_pdf(make_pdf([], positioned=_HONORARNOTE)).text
    assert re.search(r"HONORARNOTE Nr\.\s+956\s+vom\s+13/04/2026", text)
    assert re.search(r"Gesamtbetrag\s+Euro\s+670,93", text)


def test_italian_fee_note_is_recognised():
    text = analyze_pdf(make_pdf([], positioned=_HONORARNOTE)).text
    inv = ExtractedInvoice()
    inv.document_type, _, _ = classify(text)
    extract_fields(text, inv, SBC_ALIASES, ["DE815512267"])
    assert inv.document_type == DocumentType.invoice
    assert (inv.invoice_number, inv.invoice_date, inv.gross_amount, inv.currency) == (
        "956", date(2026, 4, 13), Decimal("670.93"), "EUR")
    assert inv.supplier_name == "Muster.Partner GmbH"  # nicht die Bank über der IBAN
    assert inv.supplier_vat_id == "IT01234567890"
    assert inv.missing_required() == []


def test_own_vat_in_recipient_address_is_not_own_outgoing_invoice():
    text = analyze_pdf(make_pdf([], positioned=_HONORARNOTE)).text
    assert own_vat_as_sender(text, ["DE815512267"], SBC_ALIASES) is None
    # Ohne Empfängerblock (eigene Firma nicht genannt) bleibt die Warnung.
    assert own_vat_as_sender("Muster GmbH\nUSt-ID DE815512267", ["DE815512267"], SBC_ALIASES) == "DE815512267"


def test_title_with_number_and_date_counts_as_invoice():
    lines = ["Lieferant GmbH", "Rechnung Nr. 4711 vom 02.11.2026", "Gesamtbetrag 119,00 EUR"]
    inv = ExtractedInvoice()
    inv.document_type, _, _ = classify("\n".join(lines))
    extract_fields("\n".join(lines), inv, ADF, [])
    assert inv.document_type == DocumentType.invoice
    assert (inv.invoice_number, inv.invoice_date) == ("4711", date(2026, 11, 2))


@pytest.mark.parametrize(("text", "expected"), [
    ("Invoice number INV-1\nSubtotal $10.98\nTotal $10.98\nAmount paid $10.98", "Beleg in USD"),
    ("Rechnung RE-1\nNettobetrag 100,00 EUR\nGesamtbetrag 100,00 EUR\nSteuerschuldnerschaft des Leistungsempfängers",
     "Reverse Charge laut Beleg"),
    ("Rechnung RE-1\nNettobetrag 100,00 EUR\nGesamtbetrag 100,00 EUR", "Umsatzsteuerbetrag nicht gefunden"),
])
def test_missing_vat_note_recognises_foreign_and_reverse_charge(text, expected):
    inv = ExtractedInvoice()
    inv.document_type, inv.confidence, inv.is_correction = classify(text)
    extract_fields(text, inv, ADF, [])
    assert any(expected in w for w in inv.warnings), inv.warnings


_THULE = [
    (40, 780, "Thule GmbH"), (300, 780, "RECHNUNG"),
    (40, 700, "Adfunture Outdoor GmbH"), (40, 688, "Hochstrasse 7"), (40, 676, "88131 Bodolz"),
    (40, 590, "Ihre USt-ID-Nr."), (170, 590, "Unsere USt-ID"),
    (40, 578, "DE261579742"), (170, 578, "DE133540644"),
    (40, 100, "Thule GmbH, Postfach 1467, 89304 Neumarkt"),
]


def test_own_vat_below_customer_label_in_table_is_not_outgoing_invoice():
    """Thule-Layout: Bezeichnungen stehen über den Werten. "Ihre USt-ID-Nr." gehört zur eigenen USt-ID."""
    text = analyze_pdf(make_pdf([], positioned=_THULE)).text
    assert own_vat_as_sender(text, ["DE261579742"], ADF) is None
    # Spalten vertauscht: eigene USt-ID unter "Unsere USt-ID" → weiterhin verdächtig
    swapped = [(x, y, {"DE261579742": "DE133540644", "DE133540644": "DE261579742"}.get(t, t)) for x, y, t in _THULE]
    text = analyze_pdf(make_pdf([], positioned=swapped)).text
    assert own_vat_as_sender(text, ["DE261579742"], ADF) == "DE261579742"


_THULE_AB = [(x, y, "AUFTRAGSBESTÄTIGUNG" if t == "RECHNUNG" else t) for x, y, t in _THULE] + [
    (40, 520, "Auftragsnummer"), (40, 508, "3201320707"), (40, 480, "Gesamtbetrag 423,50 EUR"),
]


def test_thule_order_confirmation_with_title_beside_letterhead_is_not_invoice():
    """Thule: Firmenname und Titel stehen in einer Zeile ("Thule GmbH AUFTRAGSBESTÄTIGUNG")."""
    text = analyze_pdf(make_pdf([], positioned=_THULE_AB)).text
    assert classify(text)[0] == DocumentType.order_confirmation
    assert non_booking_hint(text)
    assert classify(analyze_pdf(make_pdf([], positioned=_THULE)).text)[0] == DocumentType.invoice


@pytest.mark.parametrize("line", ["gemäß Auftragsbestätigung Nr. 123", "Bezug zu Lieferschein 4711", "Wir danken für Ihre Auftragsbestätigung"])
def test_references_beside_text_are_not_titles(line):
    assert classify(f"Rechnung RE-1\n{line}\nGesamtbetrag 100,00 EUR")[0] == DocumentType.invoice
