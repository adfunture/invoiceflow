"""Strukturierte E-Rechnungen: UN/CEFACT CII (ZUGFeRD, Factur-X, XRechnung-CII)
und OASIS UBL (XRechnung-UBL, Peppol).

XML wird ausschließlich mit defusedxml geparst: DTDs, externe Entitäten und
Entity-Expansion (XXE, Billion Laughs) werden abgewiesen.
"""
from __future__ import annotations

from decimal import Decimal
from xml.etree.ElementTree import Element

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring

from invoiceflow.db.enums import ClassificationMethod, DocumentType
from invoiceflow.recognition.model import (
    ExtractedInvoice,
    normalize_iban,
    normalize_vat_id,
    parse_amount,
    parse_date,
)

MAX_XML_BYTES = 20 * 1024 * 1024

NS_CII = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}
NS_UBL = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}
UBL_INVOICE = "{urn:oasis:names:specification:ubl:schema:xsd:Invoice-2}Invoice"
UBL_CREDIT = "{urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2}CreditNote"
CII_ROOT = "{%s}CrossIndustryInvoice" % NS_CII["rsm"]

# UNTDID 1001 Dokumentarten
_CREDIT_CODES = {"381", "261", "262", "296", "308", "396", "420", "458", "532"}
_CORRECTION_CODES = {"384", "471", "472", "473"}
_INVOICE_CODES = {"380", "326", "386", "389", "393", "394", "395", "456", "457", "527", "575", "623", "633", "751",
                  "780", "875", "876", "877", "935"} | _CORRECTION_CODES


class EInvoiceError(ValueError):
    pass


def _text(el: Element | None, path: str, ns: dict[str, str]) -> str | None:
    if el is None:
        return None
    found = el.find(path, ns)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value.strip()).quantize(Decimal("0.01"))
    except Exception:  # noqa: BLE001
        return parse_amount(value)


def _type_from_code(code: str | None, inv: ExtractedInvoice) -> None:
    if code in _CREDIT_CODES:
        inv.document_type = DocumentType.credit_note
    elif code in _INVOICE_CODES or code is None:
        inv.document_type = DocumentType.invoice
        inv.is_correction = code in _CORRECTION_CODES
    else:
        inv.document_type = DocumentType.unclear
        inv.warnings.append(f"Unbekannter Dokumenttyp-Code {code}")


def detect_format(data: bytes) -> str | None:
    """'cii' | 'ubl' | None (kein E-Rechnungsformat)."""
    root = parse_xml(data)
    if root.tag == CII_ROOT:
        return "cii"
    if root.tag in (UBL_INVOICE, UBL_CREDIT):
        return "ubl"
    return None


def parse_xml(data: bytes) -> Element:
    if len(data) > MAX_XML_BYTES:
        raise EInvoiceError("XML zu groß")
    try:
        return fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as exc:
        raise EInvoiceError(f"Unsicheres XML abgewiesen ({type(exc).__name__})") from exc
    except ParseError as exc:
        raise EInvoiceError(f"XML ungültig: {exc}") from exc


def parse_einvoice(data: bytes) -> ExtractedInvoice:
    root = parse_xml(data)
    if root.tag == CII_ROOT:
        return _parse_cii(root)
    if root.tag in (UBL_INVOICE, UBL_CREDIT):
        return _parse_ubl(root)
    raise EInvoiceError(f"Kein unterstütztes E-Rechnungsformat (Wurzel {root.tag})")


def _parse_cii(root: Element) -> ExtractedInvoice:
    ns = NS_CII
    inv = ExtractedInvoice(method=ClassificationMethod.xml, confidence=Decimal("1.000"))
    doc = root.find("rsm:ExchangedDocument", ns)
    _type_from_code(_text(doc, "ram:TypeCode", ns), inv)
    src = "xml"
    inv.set("invoice_number", _text(doc, "ram:ID", ns), src)
    inv.set("invoice_date", parse_date(_text(doc, "ram:IssueDateTime/udt:DateTimeString", ns)), src)

    tx = root.find("rsm:SupplyChainTradeTransaction", ns)
    agreement = tx.find("ram:ApplicableHeaderTradeAgreement", ns) if tx is not None else None
    seller = agreement.find("ram:SellerTradeParty", ns) if agreement is not None else None
    buyer = agreement.find("ram:BuyerTradeParty", ns) if agreement is not None else None
    inv.set("supplier_name", _text(seller, "ram:Name", ns), src)
    if seller is not None:
        addr = seller.find("ram:PostalTradeAddress", ns)
        parts = [_text(addr, p, ns) for p in ("ram:LineOne", "ram:LineTwo", "ram:PostcodeCode", "ram:CityName", "ram:CountryID")]
        inv.set("supplier_address", ", ".join(p for p in parts if p) or None, src)
        for reg in seller.findall("ram:SpecifiedTaxRegistration/ram:ID", ns):
            if reg.text and reg.get("schemeID") == "VA":
                inv.set("supplier_vat_id", normalize_vat_id(reg.text), src)
            elif reg.text and reg.get("schemeID") == "FC":
                inv.set("supplier_tax_number", reg.text.strip(), src)
    inv.set("recipient_name", _text(buyer, "ram:Name", ns), src)

    delivery = tx.find("ram:ApplicableHeaderTradeDelivery", ns) if tx is not None else None
    inv.set("service_date", parse_date(_text(delivery, "ram:ActualDeliverySupplyChainEvent/ram:OccurrenceDateTime/udt:DateTimeString", ns)), src)

    settlement = tx.find("ram:ApplicableHeaderTradeSettlement", ns) if tx is not None else None
    inv.set("currency", _text(settlement, "ram:InvoiceCurrencyCode", ns), src)
    totals = settlement.find("ram:SpecifiedTradeSettlementHeaderMonetarySummation", ns) if settlement is not None else None
    inv.set("net_amount", _decimal(_text(totals, "ram:TaxBasisTotalAmount", ns)), src)
    inv.set("tax_amount", _decimal(_text(totals, "ram:TaxTotalAmount", ns)), src)
    inv.set("gross_amount", _decimal(_text(totals, "ram:GrandTotalAmount", ns)), src)
    if settlement is not None:
        inv.set("iban", normalize_iban(_text(settlement, "ram:SpecifiedTradeSettlementPaymentMeans/ram:PayeePartyCreditorFinancialAccount/ram:IBANID", ns)), src)
        terms = settlement.find("ram:SpecifiedTradePaymentTerms", ns)
        inv.set("payment_terms", _text(terms, "ram:Description", ns), src)
        inv.set("due_date", parse_date(_text(terms, "ram:DueDateDateTime/udt:DateTimeString", ns)), src)
        inv.set("service_date", parse_date(_text(settlement, "ram:BillingSpecifiedPeriod/ram:StartDateTime/udt:DateTimeString", ns)), src)
        inv.set("service_period_end", parse_date(_text(settlement, "ram:BillingSpecifiedPeriod/ram:EndDateTime/udt:DateTimeString", ns)), src)
        inv.set("referenced_invoice_number", _text(settlement, "ram:InvoiceReferencedDocument/ram:IssuerAssignedID", ns), src)
    return inv


def _parse_ubl(root: Element) -> ExtractedInvoice:
    ns = NS_UBL
    inv = ExtractedInvoice(method=ClassificationMethod.xml, confidence=Decimal("1.000"))
    is_credit = root.tag == UBL_CREDIT
    code = _text(root, "cbc:CreditNoteTypeCode" if is_credit else "cbc:InvoiceTypeCode", ns)
    _type_from_code(code or ("381" if is_credit else "380"), inv)
    src = "xml"
    inv.set("invoice_number", _text(root, "cbc:ID", ns), src)
    inv.set("invoice_date", parse_date(_text(root, "cbc:IssueDate", ns)), src)
    inv.set("due_date", parse_date(_text(root, "cbc:DueDate", ns)), src)
    inv.set("currency", _text(root, "cbc:DocumentCurrencyCode", ns), src)

    seller = root.find("cac:AccountingSupplierParty/cac:Party", ns)
    inv.set("supplier_name", _text(seller, "cac:PartyLegalEntity/cbc:RegistrationName", ns)
            or _text(seller, "cac:PartyName/cbc:Name", ns), src)
    if seller is not None:
        addr = seller.find("cac:PostalAddress", ns)
        parts = [_text(addr, p, ns) for p in ("cbc:StreetName", "cbc:AdditionalStreetName", "cbc:PostalZone", "cbc:CityName", "cac:Country/cbc:IdentificationCode")]
        inv.set("supplier_address", ", ".join(p for p in parts if p) or None, src)
        for scheme in seller.findall("cac:PartyTaxScheme", ns):
            company_id = _text(scheme, "cbc:CompanyID", ns)
            if _text(scheme, "cac:TaxScheme/cbc:ID", ns) == "VAT":
                inv.set("supplier_vat_id", normalize_vat_id(company_id), src)
            elif company_id:
                inv.set("supplier_tax_number", company_id, src)
    buyer = root.find("cac:AccountingCustomerParty/cac:Party", ns)
    inv.set("recipient_name", _text(buyer, "cac:PartyLegalEntity/cbc:RegistrationName", ns)
            or _text(buyer, "cac:PartyName/cbc:Name", ns), src)

    totals = root.find("cac:LegalMonetaryTotal", ns)
    inv.set("net_amount", _decimal(_text(totals, "cbc:TaxExclusiveAmount", ns)), src)
    inv.set("gross_amount", _decimal(_text(totals, "cbc:TaxInclusiveAmount", ns)), src)
    inv.set("tax_amount", _decimal(_text(root, "cac:TaxTotal/cbc:TaxAmount", ns)), src)
    inv.set("iban", normalize_iban(_text(root, "cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID", ns)), src)
    inv.set("payment_terms", _text(root, "cac:PaymentTerms/cbc:Note", ns), src)
    inv.set("service_date", parse_date(_text(root, "cac:InvoicePeriod/cbc:StartDate", ns))
            or parse_date(_text(root, "cac:Delivery/cbc:ActualDeliveryDate", ns)), src)
    inv.set("service_period_end", parse_date(_text(root, "cac:InvoicePeriod/cbc:EndDate", ns)), src)
    inv.set("referenced_invoice_number", _text(root, "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID", ns), src)
    return inv
