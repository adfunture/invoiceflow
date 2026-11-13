"""Verarbeitung importierter Nachrichten: Dokumente anlegen, erkennen, prüfen, entscheiden.

Ablauf je Dokument
------------------
1. Original aus dem Speicher laden (Hash wird geprüft).
2. Analyse: E-Rechnungs-XML (ZUGFeRD/Factur-X/XRechnung) → strukturierte Daten;
   sonst PDF-Text bzw. OCR → Regeln; bei Unklarheit optional KI.
3. Entscheidung (rein fachlich, ohne DB): ignoriert / manuelle Prüfung /
   bereit für DATEV / XML-Warteschlange.
4. In **einer** Transaktion unter Advisory-Locks (Datei-Hash und
   Lieferant+Rechnungsnummer): Dublettenprüfung Ebene 1 und 2, dann Speichern.
   Zwei Worker, die dieselbe Rechnung gleichzeitig verarbeiten, werden
   dadurch serialisiert – der zweite sieht die Entscheidung des ersten.

Nachrichten ohne verwertbaren Anhang werden nicht verworfen: Rechnungslinks
oder Rechnungshinweise im Text führen zur manuellen Prüfung.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlsplit

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from invoiceflow.db.enums import (
    AttachmentSafety,
    ClassificationMethod,
    DocumentStatus,
    DocumentType,
    DuplicateDecision,
    DuplicateMatchLevel,
    EmailMessageStatus,
    SourceKind,
)
from invoiceflow.db.models import (
    Company,
    Document,
    EmailAttachment,
    EmailMessage,
    InvoiceDuplicate,
    InvoiceMetadata,
)
from invoiceflow.mail import link_fetch
from invoiceflow.mail.mime import classify_attachment, is_dangerous
from invoiceflow.recognition import ai as ai_mod
from invoiceflow.recognition.einvoice import EInvoiceError, parse_einvoice
from invoiceflow.recognition.model import (
    INVOICE_LIKE,
    ExtractedInvoice,
    identity_hash,
    normalize_invoice_number,
    normalize_vat_id,
    supplier_key,
)
from invoiceflow.recognition.ocr import OcrProvider, OcrUnavailable, get_ocr
from invoiceflow.recognition.pdf import PdfError, analyze_pdf
from invoiceflow.recognition.recipient import CompanyNames, recipient_mismatch
from invoiceflow.recognition.text_rules import classify, extract_fields, non_booking_hint, own_vat_as_sender
from invoiceflow.services import app_settings
from invoiceflow.storage import DocumentStore, IntegrityError as StorageIntegrityError

log = logging.getLogger("invoiceflow.recognition")

TERMINAL = {
    DocumentStatus.ready_for_datev, DocumentStatus.awaiting_datev_xml_integration, DocumentStatus.manual_review,
    DocumentStatus.duplicate, DocumentStatus.ignored, DocumentStatus.transmitted, DocumentStatus.error,
}
# Dokumente, deren Entscheidung als "bereits vorhanden" für die Dublettenprüfung zählt.
DECIDED = TERMINAL - {DocumentStatus.duplicate, DocumentStatus.error}
NON_BOOKING = {DocumentType.offer, DocumentType.delivery_note, DocumentType.order_confirmation, DocumentType.other}
_TYPE_LABELS = {
    DocumentType.offer: "Angebot", DocumentType.delivery_note: "Lieferschein",
    DocumentType.order_confirmation: "Auftragsbestätigung", DocumentType.other: "sonstiges Dokument",
}


@dataclass
class Analysis:
    source_kind: SourceKind
    invoice: ExtractedInvoice | None = None
    text: str = ""
    page_count: int | None = None
    review_reasons: list[str] = field(default_factory=list)
    embedded_xml: bytes | None = None
    embedded_xml_name: str | None = None
    ai_used: bool = False
    ai_confidence: float | None = None


@dataclass
class Decision:
    status: DocumentStatus
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CompanyContext:
    company_id: int
    slug: str
    own: CompanyNames
    others: list[CompanyNames]
    own_vat_ids: list[str]


def _now() -> datetime:
    return datetime.now(UTC)


class DocumentProcessor:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        store: DocumentStore,
        *,
        ai_factory: Callable[[str | None], ai_mod.AiProvider] = ai_mod.get_provider,
        ocr_factory: Callable[[str | None], OcrProvider] = get_ocr,
        link_fetcher: link_fetch.Fetcher = link_fetch.fetch_invoice,
    ) -> None:
        self.sessions = sessions
        self.store = store
        self.ai_factory = ai_factory
        self.ocr_factory = ocr_factory
        self.link_fetcher = link_fetcher

    # --- Nachricht ----------------------------------------------------------

    def process_message(self, message_id: int) -> EmailMessageStatus:
        self._fetch_links(message_id)
        with self.sessions() as db:
            msg = db.get(EmailMessage, message_id)
            if msg is None:
                raise ValueError(f"Nachricht {message_id} existiert nicht")
            attachments = list(db.scalars(select(EmailAttachment).where(EmailAttachment.email_message_id == msg.id)
                                          .order_by(EmailAttachment.part_index)))
            for att in attachments:
                if att.safety_status == AttachmentSafety.ok and att.storage_key:
                    db.execute(insert(Document).values(
                        company_id=msg.company_id, email_message_id=msg.id, attachment_id=att.id, sha256=att.sha256,
                        size_bytes=att.size_bytes, storage_key=att.storage_key, original_filename=att.filename,
                        mime_type=att.detected_content_type, source_kind=_initial_kind(att.detected_content_type),
                    ).on_conflict_do_nothing(constraint="uq_documents_attachment"))
            if msg.status == EmailMessageStatus.fetched:
                msg.status = EmailMessageStatus.processing
            db.commit()
            doc_ids = list(db.scalars(select(Document.id).where(Document.email_message_id == msg.id).order_by(Document.id)))

        for doc_id in doc_ids:
            self.process_document(doc_id)
        return self._finalize_message(message_id)

    def _fetch_links(self, message_id: int) -> None:
        """Rechnungslinks auf freigegebenen Domains laden und wie Anhänge ablegen (einmal je Link)."""
        with self.sessions() as db:
            msg = db.get(EmailMessage, message_id)
            if msg is None or not msg.detected_invoice_links:
                return
            domains = list(app_settings.get_setting(db, "links.auto_fetch_domains", msg.company_id) or [])
            if not domains:
                return
            existing = list(db.scalars(select(EmailAttachment).where(EmailAttachment.email_message_id == msg.id)))
            if any(a.safety_status == AttachmentSafety.ok and a.storage_key and not (a.content_id or "").startswith("link:")
                   for a in existing):
                return  # echter Anhang vorhanden – Link ist nur ein Zusatz
            tried = {a.content_id for a in existing if (a.content_id or "").startswith("link:")}
            max_bytes = int(app_settings.get_setting(db, "attachments.max_size_mb", msg.company_id)) * 1024 * 1024
            slug = db.scalar(select(Company.slug).where(Company.id == msg.company_id))
            company_id, links = msg.company_id, list(msg.detected_invoice_links)
            next_part = 1000 + len(tried)

        rows: list[dict] = []
        for url in links:
            key = "link:" + hashlib.sha256(url.encode()).hexdigest()[:32]
            if key in tried or not link_fetch.allowed(url, domains):
                continue
            tried.add(key)
            row = dict(company_id=company_id, email_message_id=message_id, part_index=next_part, content_id=key)
            next_part += 1
            try:
                fetched = self.link_fetcher(url, domains, max_bytes)
            except Exception as exc:  # noqa: BLE001 – Abruf scheitert: Mail bleibt mit Grund in der Prüfung
                reason = str(exc) if isinstance(exc, link_fetch.LinkFetchError) else type(exc).__name__
                rows.append(row | dict(filename=urlsplit(url).hostname, size_bytes=0, safety_status=AttachmentSafety.corrupt,
                                       rejection_reason=f"Link-Abruf fehlgeschlagen: {reason}"[:1000]))
                continue
            safety, detected, reason = classify_attachment(fetched.filename, fetched.data, max_bytes)
            row |= dict(filename=fetched.filename, declared_content_type=fetched.content_type,
                        detected_content_type=detected, size_bytes=len(fetched.data), safety_status=safety,
                        rejection_reason=reason)
            if safety == AttachmentSafety.ok:
                obj = self.store.put(slug, "attachment", fetched.data, fetched.filename.rsplit(".", 1)[-1])
                row |= dict(sha256=obj.sha256, storage_key=obj.key)
            rows.append(row)
            log.info("invoice_link_fetched", extra={"email_message_id": message_id, "ok": safety == AttachmentSafety.ok})
        if rows:
            with self.sessions() as db:
                for row in rows:
                    db.execute(insert(EmailAttachment).values(**row).on_conflict_do_nothing())
                db.commit()

    def _finalize_message(self, message_id: int) -> EmailMessageStatus:
        with self.sessions() as db:
            msg = db.get(EmailMessage, message_id)
            docs = list(db.scalars(select(Document).where(Document.email_message_id == msg.id)))
            attachments = list(db.scalars(select(EmailAttachment).where(EmailAttachment.email_message_id == msg.id)))
            reasons: list[str] = []
            if msg.status == EmailMessageStatus.error or (msg.review_reason and msg.raw_storage_key is None):
                reasons.append(msg.review_reason or "Nachricht nicht lesbar")
            for att in attachments:
                name = att.filename or f"Teil {att.part_index}"
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if att.safety_status in (AttachmentSafety.corrupt, AttachmentSafety.rejected_size):
                    reasons.append(f"Anhang „{name}“: {att.rejection_reason}")
                elif att.safety_status == AttachmentSafety.rejected_type and is_dangerous(att.filename, att.detected_content_type):
                    reasons.append(f"Gefährlicher Anhang „{name}“ blockiert")
                elif att.safety_status == AttachmentSafety.rejected_type and ext == "zip":
                    reasons.append(f"Anhang „{name}“: {att.rejection_reason}")
            # Dokumente in Prüfung stehen in der Dokumentliste; die E-Mail selbst braucht dafür keinen Prüfvermerk.
            if not docs and (msg.detected_invoice_links or msg.body_mentions_invoice):
                reasons.append("Kein Rechnungsanhang, aber Rechnungslink/-hinweis im Text – bitte prüfen"
                               + (f" ({len(msg.detected_invoice_links)} Link(s), nicht automatisch abgerufen)"
                                  if msg.detected_invoice_links else ""))
            if reasons:
                msg.status = EmailMessageStatus.manual_review
                msg.review_reason = "; ".join(dict.fromkeys(reasons))[:4000]
            else:
                msg.status = EmailMessageStatus.processed if docs else EmailMessageStatus.no_invoice
            db.commit()
            return msg.status

    # --- Dokument -----------------------------------------------------------

    def process_document(self, document_id: int) -> DocumentStatus:
        with self.sessions() as db:
            doc = db.get(Document, document_id)
            if doc.status in TERMINAL:
                return doc.status
            doc.status = DocumentStatus.processing
            ctx = self._company_context(db, doc.company_id)
            ai_enabled = bool(app_settings.get_setting(db, "ai.enabled", doc.company_id))
            ai_provider = app_settings.get_setting(db, "ai.provider", doc.company_id)
            ai_min = int(app_settings.get_setting(db, "ai.min_confidence_percent", doc.company_id)) / 100
            ocr_provider = app_settings.get_setting(db, "ocr.provider", doc.company_id)
            sender, auth = db.execute(select(EmailMessage.from_address, EmailMessage.sender_auth)
                                      .where(EmailMessage.id == doc.email_message_id)).one()
            invoice_sender = app_settings.sender_matches(
                sender, app_settings.get_setting(db, "recognition.invoice_senders", doc.company_id) or [])
            # Positive Regel nur für echte Absender – das From-Feld allein lässt sich fälschen.
            sender_verified = auth == "pass"
            storage_key, sha, mime, filename = doc.storage_key, doc.sha256, doc.mime_type, doc.original_filename
            db.commit()

        try:
            data = self.store.get(storage_key, sha)
        except StorageIntegrityError as exc:
            return self._save_error(document_id, f"Original im Speicher beschädigt: {exc}")

        analysis = self._analyze(data, mime, ctx, ocr_provider)
        inv = analysis.invoice
        if inv is not None and invoice_sender and inv.document_type == DocumentType.unclear:
            if non_booking_hint(analysis.text):
                inv.warnings.append("Absenderregel nicht angewendet: Kopfbereich nennt Auftragsbestätigung/Lieferschein")
            elif sender_verified:
                # Absenderregel (beim Freigeben gelernt): unklare Belege dieses Absenders sind Rechnungen.
                inv.document_type = DocumentType.invoice
                inv.warnings.append("Dokumentart über Absenderregel als Rechnung erkannt")
            else:
                inv.warnings.append("Absenderregel nicht angewendet: Absender nicht per SPF/DKIM/DMARC bestätigt")
        if inv is not None and ai_enabled and _needs_ai(inv):
            self._ask_ai(analysis, ai_provider, ai_min)
        decision = decide(analysis, ctx, ai_min)
        if analysis.embedded_xml:
            embedded = self.store.put(ctx.slug, "embedded_xml", analysis.embedded_xml, "xml")
        else:
            embedded = None
        return self._persist(document_id, ctx, analysis, decision, embedded.key if embedded else None)

    def _company_context(self, db: Session, company_id: int) -> CompanyContext:
        companies = list(db.scalars(select(Company)))
        names = {c.id: CompanyNames(c.id, c.short_name, [c.legal_name, c.short_name, *c.recipient_aliases]) for c in companies}
        own = next(c for c in companies if c.id == company_id)
        return CompanyContext(
            company_id=company_id, slug=own.slug, own=names[company_id],
            others=[n for cid, n in names.items() if cid != company_id],
            own_vat_ids=[own.vat_id] if own.vat_id else [],
        )

    def _analyze(self, data: bytes, mime: str | None, ctx: CompanyContext, ocr_provider: str | None) -> Analysis:
        own_aliases = ctx.own.aliases
        if mime == "application/pdf":
            try:
                pdf = analyze_pdf(data)
            except PdfError as exc:
                return Analysis(SourceKind.pdf, review_reasons=[str(exc)])
            analysis = Analysis(SourceKind.pdf, text=pdf.text, page_count=pdf.page_count)
            if pdf.embedded_xml:
                try:
                    analysis.invoice = parse_einvoice(pdf.embedded_xml)
                    analysis.source_kind = SourceKind.zugferd_pdf
                    analysis.embedded_xml, analysis.embedded_xml_name = pdf.embedded_xml, pdf.embedded_xml_name
                except EInvoiceError as exc:
                    analysis.review_reasons.append(f"Eingebettete XML ({pdf.embedded_xml_name}) nicht verwertbar: {exc}")
            if analysis.invoice is None and pdf.is_scan:
                analysis.source_kind = SourceKind.scan_pdf
                analysis.text = self._ocr(analysis, lambda o: o.pdf_to_text(data), ocr_provider)
                if not analysis.text:
                    return analysis
                source = "ocr"
            else:
                source = "text"
        elif mime == "application/xml":
            try:
                inv = parse_einvoice(data)
            except EInvoiceError as exc:
                return Analysis(SourceKind.xml_other, review_reasons=[f"XML ist keine verwertbare E-Rechnung: {exc}"])
            return Analysis(SourceKind.xml_xrechnung, invoice=inv)
        elif mime in ("image/png", "image/jpeg", "image/tiff"):
            analysis = Analysis(SourceKind.image)
            analysis.text = self._ocr(analysis, lambda o: o.image_to_text(data), ocr_provider)
            if not analysis.text:
                return analysis
            source = "ocr"
        else:
            return Analysis(SourceKind.unknown, review_reasons=[f"Dateityp {mime or 'unbekannt'} – bitte manuell prüfen"])

        if analysis.invoice is None:
            inv = ExtractedInvoice()
            inv.document_type, inv.confidence, inv.is_correction = classify(analysis.text)
            analysis.invoice = inv
        # Bei E-Rechnungen ergänzt der Text nur fehlende Felder; XML-Werte haben Vorrang.
        extract_fields(analysis.text, analysis.invoice, own_aliases, ctx.own_vat_ids, source=source)
        return analysis

    def _ocr(self, analysis: Analysis, run: Callable[[OcrProvider], str], provider: str | None) -> str:
        try:
            ocr = self.ocr_factory(provider)
        except OcrUnavailable as exc:
            analysis.review_reasons.append(f"Scan ohne Textebene – {exc}")
            return ""
        try:
            text_out = run(ocr)
        except Exception as exc:  # noqa: BLE001 – kaputtes Bild etc.
            analysis.review_reasons.append(f"OCR fehlgeschlagen: {type(exc).__name__}")
            return ""
        if len(text_out.strip()) < 20:
            analysis.review_reasons.append("OCR hat keinen verwertbaren Text erkannt")
            return ""
        return text_out

    def _ask_ai(self, analysis: Analysis, provider_name: str | None, min_confidence: float) -> None:
        inv = analysis.invoice
        reason = ai_mod.ai_input_ok(analysis.text)
        if reason:
            inv.warnings.append(f"KI nicht befragt: {reason}")
            return
        try:
            provider = self.ai_factory(provider_name)
            result = provider.extract(analysis.text)
        except ai_mod.AiError as exc:
            inv.warnings.append(f"KI-Fallback nicht verfügbar: {exc}")
            return
        ai_mod.apply_ai(result, analysis.text, inv, min_confidence)
        analysis.ai_used = True
        analysis.ai_confidence = result.confidence

    # --- Speichern mit Dublettenprüfung -------------------------------------

    def _save_error(self, document_id: int, message: str) -> DocumentStatus:
        with self.sessions() as db:
            doc = db.get(Document, document_id)
            doc.status = DocumentStatus.manual_review
            doc.error_message = message[:4000]
            doc.review_reason = message[:4000]
            db.commit()
        return DocumentStatus.manual_review

    def _persist(self, document_id: int, ctx: CompanyContext, analysis: Analysis, decision: Decision,
                 embedded_key: str | None) -> DocumentStatus:
        inv = analysis.invoice
        ident = identity_hash(ctx.company_id, inv) if inv else None
        skey = supplier_key(inv) if inv else None
        number = normalize_invoice_number(inv.invoice_number) if inv else None

        with self.sessions() as db:
            doc = db.scalar(select(Document).where(Document.id == document_id).with_for_update())
            if doc.status in TERMINAL:  # anderer Worker war schneller (Lease-Neuvergabe)
                db.rollback()
                return doc.status
            lock_keys = sorted({f"file:{ctx.company_id}:{doc.sha256}"} | (
                {f"inv:{ctx.company_id}:{skey}:{number}"} if skey and number else set()))
            for key in lock_keys:
                db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})

            dup_rows: list[InvoiceDuplicate] = []
            # Ebene 1: identische Datei bereits entschieden?
            same_file = db.scalar(
                select(Document.id).where(Document.company_id == ctx.company_id, Document.sha256 == doc.sha256,
                                          Document.id != doc.id, Document.status.in_(DECIDED))
                .order_by(Document.id).limit(1))
            if same_file is not None:
                decision = Decision(DocumentStatus.duplicate, [f"Identische Datei wie Dokument {same_file}"])
                dup_rows.append(InvoiceDuplicate(company_id=ctx.company_id, document_id=doc.id, duplicate_of_document_id=same_file,
                                                 match_level=DuplicateMatchLevel.file_hash,
                                                 decision=DuplicateDecision.confirmed_duplicate))
            elif inv is not None and inv.document_type in INVOICE_LIKE:
                # Ebene 2: gleiche Rechnungsidentität (anderer Dateiinhalt, z. B. neu erzeugtes PDF)?
                if ident:
                    same_invoice = db.scalar(
                        select(Document.id).join(InvoiceMetadata, InvoiceMetadata.document_id == Document.id)
                        .where(InvoiceMetadata.company_id == ctx.company_id, InvoiceMetadata.identity_hash == ident,
                               Document.id != doc.id, Document.status.in_(DECIDED))
                        .order_by(Document.id).limit(1))
                    if same_invoice is not None:
                        decision = Decision(DocumentStatus.duplicate, [f"Gleiche Rechnung wie Dokument {same_invoice}"])
                        dup_rows.append(InvoiceDuplicate(company_id=ctx.company_id, document_id=doc.id,
                                                         duplicate_of_document_id=same_invoice,
                                                         match_level=DuplicateMatchLevel.invoice_identity,
                                                         decision=DuplicateDecision.confirmed_duplicate))
                # Konflikt: gleiche Nummer beim selben Lieferanten, aber abweichende Daten (Korrektur?).
                if decision.status != DocumentStatus.duplicate and skey and number:
                    conflicts = db.execute(
                        select(Document.id, InvoiceMetadata.gross_amount, InvoiceMetadata.invoice_date)
                        .join(InvoiceMetadata, InvoiceMetadata.document_id == Document.id)
                        .where(InvoiceMetadata.company_id == ctx.company_id, InvoiceMetadata.supplier_key == skey,
                               InvoiceMetadata.invoice_number_normalized == number,
                               InvoiceMetadata.document_type == inv.document_type,
                               Document.id != doc.id, Document.status.in_(DECIDED))
                    ).all()
                    for other_id, other_gross, other_date in conflicts:
                        dup_rows.append(InvoiceDuplicate(
                            company_id=ctx.company_id, document_id=doc.id, duplicate_of_document_id=other_id,
                            match_level=DuplicateMatchLevel.possible, decision=DuplicateDecision.needs_review,
                            details={"other_gross": str(other_gross), "other_date": str(other_date),
                                     "this_gross": str(inv.gross_amount), "this_date": str(inv.invoice_date)}))
                    if conflicts:
                        decision = Decision(DocumentStatus.manual_review, decision.reasons + [
                            "Gleiche Rechnungsnummer wie Dokument " + ", ".join(str(c[0]) for c in conflicts)
                            + " mit abweichenden Daten (Korrektur?) – bitte prüfen"])

            doc.status = decision.status
            doc.review_reason = "; ".join(decision.reasons)[:4000] or None
            doc.source_kind = analysis.source_kind
            doc.page_count = analysis.page_count
            doc.embedded_xml_storage_key = embedded_key
            doc.embedded_xml_filename = analysis.embedded_xml_name
            if inv is not None:
                doc.document_type = inv.document_type
                doc.classification_method = inv.method
                doc.ai_confidence = (Decimal(str(round(analysis.ai_confidence, 3)))
                                     if analysis.ai_confidence is not None else None)
                self._save_metadata(db, doc, inv, ident, skey, number, analysis)
            for row in dup_rows:
                db.execute(insert(InvoiceDuplicate).values(
                    company_id=row.company_id, document_id=row.document_id,
                    duplicate_of_document_id=row.duplicate_of_document_id, match_level=row.match_level,
                    decision=row.decision, details=row.details or {},
                ).on_conflict_do_nothing(constraint="uq_invoice_duplicates_pair"))
            db.commit()
            log.info("document_processed", extra={"document_id": doc.id, "status": doc.status.value,
                                                  "type": doc.document_type.value, "kind": doc.source_kind.value})
            return doc.status

    @staticmethod
    def _save_metadata(db: Session, doc: Document, inv: ExtractedInvoice, ident: str | None, skey: str | None,
                       number: str | None, analysis: Analysis) -> None:
        values = dict(
            company_id=doc.company_id, document_id=doc.id, document_type=inv.document_type,
            invoice_number=inv.invoice_number, invoice_number_normalized=number, invoice_date=inv.invoice_date,
            service_date=inv.service_date, service_period_end=inv.service_period_end,
            supplier_name=(inv.supplier_name or None) and inv.supplier_name[:500],
            supplier_address=inv.supplier_address, supplier_vat_id=inv.supplier_vat_id,
            supplier_tax_number=inv.supplier_tax_number, supplier_key=skey and skey[:200],
            net_amount=inv.net_amount, tax_amount=inv.tax_amount, gross_amount=inv.gross_amount,
            currency=inv.currency if inv.currency and len(inv.currency) == 3 else None,
            recipient_name=inv.recipient_name and inv.recipient_name[:500], recipient_text=inv.recipient_text,
            iban=inv.iban, payment_terms=inv.payment_terms, due_date=inv.due_date,
            referenced_invoice_number=inv.referenced_invoice_number, extraction_method=inv.method,
            confidence=inv.confidence, identity_hash=ident, field_sources=inv.field_sources,
            raw_extraction={"warnings": inv.warnings, "is_correction": inv.is_correction,
                            "other_invoice_numbers": inv.other_invoice_numbers, "ai_used": analysis.ai_used},
        )
        stmt = insert(InvoiceMetadata).values(**values)
        db.execute(stmt.on_conflict_do_update(
            constraint="uq_invoice_metadata_document",
            set_={k: stmt.excluded[k] for k in values if k not in ("company_id", "document_id")} | {"updated_at": func.now()},
        ))


def _initial_kind(mime: str | None) -> SourceKind:
    return {
        "application/pdf": SourceKind.pdf,
        "application/xml": SourceKind.xml_other,
        "image/png": SourceKind.image,
        "image/jpeg": SourceKind.image,
        "image/tiff": SourceKind.image,
    }.get(mime or "", SourceKind.unknown)


def _needs_ai(inv: ExtractedInvoice) -> bool:
    if inv.method == ClassificationMethod.xml:
        return False
    return inv.document_type == DocumentType.unclear or (inv.document_type in INVOICE_LIKE and bool(inv.missing_required()))


def decide(analysis: Analysis, ctx: CompanyContext, ai_min_confidence: float) -> Decision:
    """Fachliche Entscheidung ohne Datenbank. Dubletten werden danach in ``_persist`` geprüft."""
    reasons = list(analysis.review_reasons)
    inv = analysis.invoice
    if inv is None:
        return Decision(DocumentStatus.manual_review, reasons or ["Dokument nicht auswertbar"])
    if reasons:
        return Decision(DocumentStatus.manual_review, reasons)
    if inv.document_type in NON_BOOKING:
        return Decision(DocumentStatus.ignored, [f"Kein Buchungsbeleg ({_TYPE_LABELS[inv.document_type]})"])
    if inv.document_type == DocumentType.dunning:
        return Decision(DocumentStatus.manual_review, ["Mahnung – prüfen, ob die zugrunde liegende Rechnung vorliegt"])
    if inv.document_type == DocumentType.unclear:
        return Decision(DocumentStatus.manual_review, ["Dokumentart nicht eindeutig erkannt"])

    if inv.other_invoice_numbers and inv.method != ClassificationMethod.xml:
        reasons.append("Mehrere Rechnungsnummern in einem Dokument (" + ", ".join([inv.invoice_number or "?", *inv.other_invoice_numbers]) + ")")
    missing = inv.missing_required()
    if missing:
        labels = {"invoice_number": "Rechnungsnummer", "invoice_date": "Rechnungsdatum", "gross_amount": "Bruttobetrag",
                  "currency": "Währung", "supplier": "Lieferant"}
        reasons.append("Pflichtangaben fehlen: " + ", ".join(labels[m] for m in missing))
    own_vats = {normalize_vat_id(v) for v in ctx.own_vat_ids}
    if (inv.supplier_vat_id and inv.supplier_vat_id in own_vats) or own_vat_as_sender(analysis.text, ctx.own_vat_ids, ctx.own.aliases):
        reasons.append(f"Möglicherweise eigene Ausgangsrechnung ({ctx.own.short_name} ist Rechnungssteller) – "
                       "nicht als Eingangsrechnung an DATEV senden")
    mismatch = recipient_mismatch(ctx.own, ctx.others, analysis.text, inv.recipient_name)
    if mismatch:
        reasons.append(mismatch)
    if inv.amounts_consistent() is False:
        reasons.append(f"Netto ({inv.net_amount}) + USt ({inv.tax_amount}) ≠ Brutto ({inv.gross_amount})")
    sources = {inv.field_sources.get(f) for f in ("invoice_number", "invoice_date", "gross_amount")}
    if "ai" in sources or inv.method == ClassificationMethod.ai:
        if (analysis.ai_confidence or 0) < ai_min_confidence:
            reasons.append("KI-Ergebnis unter der Mindestkonfidenz")
    if "ocr" in sources and inv.amounts_consistent() is not True:
        reasons.append("OCR-Werte nicht durch Summenprüfung (Netto + USt = Brutto) bestätigt")
    if reasons:
        return Decision(DocumentStatus.manual_review, reasons)
    if analysis.source_kind in (SourceKind.xml_xrechnung, SourceKind.xml_other):
        return Decision(DocumentStatus.awaiting_datev_xml_integration,
                        ["Reine XML-Rechnung: wartet auf DATEV-XML-Schnittstelle"])
    return Decision(DocumentStatus.ready_for_datev)
