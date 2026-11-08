"""Testbelege: echte, mit pypdf lesbare PDFs (Text, Scan, ZUGFeRD) und E-Rechnungs-XML."""
from __future__ import annotations

import io

from pypdf import PdfReader, PdfWriter


def _escape(text: str) -> bytes:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("cp1252")


def make_pdf(pages: list[list[str]] | list[str], *, producer: str = "InvoiceFlow-Test") -> bytes:
    """Mehrseitiges PDF; jede Seite ist eine Liste von Textzeilen. Leere Seite = Scan ohne Textebene."""
    if pages and isinstance(pages[0], str):
        pages = [pages]  # type: ignore[list-item]
    objects: list[bytes] = []
    kids = []
    font_id = 3
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"")  # Platzhalter Pages
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for lines in pages:
        stream = b"BT /F1 10 Tf 14 TL 50 800 Td " + b" ".join(b"(" + _escape(l) + b") Tj T*" for l in lines) + b" ET"
        if not lines:
            stream = b"q 0 0 0 rg 50 50 100 100 re f Q"  # nur Grafik, kein Text
        content_id = len(objects) + 1
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_id = len(objects) + 1
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                       % (font_id, content_id))
        kids.append(page_id)
    objects[1] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)
    info_id = len(objects) + 1
    objects.append(b"<< /Producer (" + _escape(producer) + b") >>")

    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, info_id, xref))
    return out.getvalue()


def with_embedded_xml(pdf: bytes, xml: bytes, name: str = "factur-x.xml") -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    writer.add_attachment(name, xml)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def invoice_lines(
    number: str = "RE-2026-1001",
    *,
    supplier: str = "Müller Outdoor Handel GmbH",
    recipient: str = "Adfunture Outdoor GmbH",
    title: str = "Rechnung",
    date: str = "09.11.2026",
    net: str = "1.000,00",
    tax: str = "190,00",
    gross: str = "1.190,00",
    vat: str = "DE123456789",
    extra: list[str] | None = None,
) -> list[str]:
    return [
        f"{supplier} - Industriestr. 5 - 80331 München",
        recipient,
        "Musterweg 1",
        "12345 Musterstadt",
        title,
        f"Rechnungsnummer: {number}",
        f"Rechnungsdatum: {date}",
        "Leistungsdatum: 05.11.2026",
        "Pos. 1 Zelte Modell Alpin 10 Stk",
        f"Nettobetrag {net} EUR",
        f"MwSt 19% {tax} EUR",
        f"Gesamtbetrag {gross} EUR",
        *(extra or []),
        "Zahlbar innerhalb 14 Tagen ohne Abzug.",
        f"{supplier} | USt-IdNr.: {vat} | IBAN: DE89 3704 0044 0532 0130 00",
    ]


def cii_xml(
    number: str = "ZF-2026-77",
    *,
    type_code: str = "380",
    issue: str = "20261109",
    seller: str = "Zeltbau Nord GmbH &amp; Co. KG",
    seller_vat: str = "DE987654321",
    buyer: str = "Adfunture Outdoor GmbH",
    net: str = "500.00",
    tax: str = "95.00",
    gross: str = "595.00",
) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
 xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
 xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
 <rsm:ExchangedDocument>
  <ram:ID>{number}</ram:ID><ram:TypeCode>{type_code}</ram:TypeCode>
  <ram:IssueDateTime><udt:DateTimeString format="102">{issue}</udt:DateTimeString></ram:IssueDateTime>
 </rsm:ExchangedDocument>
 <rsm:SupplyChainTradeTransaction>
  <ram:ApplicableHeaderTradeAgreement>
   <ram:SellerTradeParty><ram:Name>{seller}</ram:Name>
    <ram:PostalTradeAddress><ram:PostcodeCode>10115</ram:PostcodeCode><ram:LineOne>Zeltweg 3</ram:LineOne><ram:CityName>Berlin</ram:CityName><ram:CountryID>DE</ram:CountryID></ram:PostalTradeAddress>
    <ram:SpecifiedTaxRegistration><ram:ID schemeID="VA">{seller_vat}</ram:ID></ram:SpecifiedTaxRegistration>
   </ram:SellerTradeParty>
   <ram:BuyerTradeParty><ram:Name>{buyer}</ram:Name></ram:BuyerTradeParty>
  </ram:ApplicableHeaderTradeAgreement>
  <ram:ApplicableHeaderTradeDelivery/>
  <ram:ApplicableHeaderTradeSettlement>
   <ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>
   <ram:SpecifiedTradeSettlementPaymentMeans><ram:PayeePartyCreditorFinancialAccount><ram:IBANID>DE89370400440532013000</ram:IBANID></ram:PayeePartyCreditorFinancialAccount></ram:SpecifiedTradeSettlementPaymentMeans>
   <ram:SpecifiedTradePaymentTerms><ram:DueDateDateTime><udt:DateTimeString format="102">20261123</udt:DateTimeString></ram:DueDateDateTime></ram:SpecifiedTradePaymentTerms>
   <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
    <ram:TaxBasisTotalAmount>{net}</ram:TaxBasisTotalAmount>
    <ram:TaxTotalAmount currencyID="EUR">{tax}</ram:TaxTotalAmount>
    <ram:GrandTotalAmount>{gross}</ram:GrandTotalAmount>
    <ram:DuePayableAmount>{gross}</ram:DuePayableAmount>
   </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
  </ram:ApplicableHeaderTradeSettlement>
 </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>""".encode()


def ubl_xml(number: str = "XR-500", *, buyer: str = "Adfunture Outdoor GmbH", gross: str = "238.00") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
 <cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:xeinkauf.de:kosit:xrechnung_3.0</cbc:CustomizationID>
 <cbc:ID>{number}</cbc:ID>
 <cbc:IssueDate>2026-11-10</cbc:IssueDate>
 <cbc:DueDate>2026-11-24</cbc:DueDate>
 <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
 <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
 <cac:AccountingSupplierParty><cac:Party>
  <cac:PostalAddress><cbc:StreetName>Amtsweg 1</cbc:StreetName><cbc:CityName>Köln</cbc:CityName><cbc:PostalZone>50667</cbc:PostalZone><cac:Country><cbc:IdentificationCode>DE</cbc:IdentificationCode></cac:Country></cac:PostalAddress>
  <cac:PartyTaxScheme><cbc:CompanyID>DE111222333</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
  <cac:PartyLegalEntity><cbc:RegistrationName>Stadtwerke Beispiel GmbH</cbc:RegistrationName></cac:PartyLegalEntity>
 </cac:Party></cac:AccountingSupplierParty>
 <cac:AccountingCustomerParty><cac:Party><cac:PartyLegalEntity><cbc:RegistrationName>{buyer}</cbc:RegistrationName></cac:PartyLegalEntity></cac:Party></cac:AccountingCustomerParty>
 <cac:TaxTotal><cbc:TaxAmount currencyID="EUR">38.00</cbc:TaxAmount></cac:TaxTotal>
 <cac:LegalMonetaryTotal>
  <cbc:TaxExclusiveAmount currencyID="EUR">200.00</cbc:TaxExclusiveAmount>
  <cbc:TaxInclusiveAmount currencyID="EUR">{gross}</cbc:TaxInclusiveAmount>
  <cbc:PayableAmount currencyID="EUR">{gross}</cbc:PayableAmount>
 </cac:LegalMonetaryTotal>
</Invoice>""".encode()
