"""Manuelle Prüfung von Dokumenten.

Die Unternehmenszuordnung kann hier nicht geändert werden. Gehört ein Beleg
zu einem anderen Unternehmen, wird er ignoriert (mit Begründung) und dort
eingereicht.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from invoiceflow.db.enums import (
    ClassificationMethod,
    DocumentStatus,
    DocumentType,
    DuplicateDecision,
    DuplicateMatchLevel,
    SourceKind,
)
from invoiceflow.db.models import Document, InvoiceDuplicate, InvoiceMetadata
from invoiceflow.recognition.model import (
    INVOICE_LIKE,
    ExtractedInvoice,
    identity_hash,
    normalize_invoice_number,
    normalize_vat_id,
    supplier_key,
)
from invoiceflow.services.audit import Actor, audit

REVIEWABLE = {DocumentStatus.manual_review, DocumentStatus.duplicate, DocumentStatus.ignored}
LIVE = {DocumentStatus.ready_for_datev, DocumentStatus.transmitted, DocumentStatus.awaiting_datev_xml_integration}
CORRECTABLE = ("document_type", "invoice_number", "invoice_date", "service_date", "supplier_name", "supplier_vat_id",
               "net_amount", "tax_amount", "gross_amount", "currency", "due_date", "iban", "referenced_invoice_number")


class ReviewError(ValueError):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def _to_invoice(meta: InvoiceMetadata) -> ExtractedInvoice:
    inv = ExtractedInvoice(document_type=meta.document_type)
    for name in ("invoice_number", "invoice_date", "supplier_name", "supplier_vat_id", "gross_amount", "currency",
                 "net_amount", "tax_amount"):
        setattr(inv, name, getattr(meta, name))
    return inv


_DATE_FIELDS = {"invoice_date", "service_date", "due_date"}
_AMOUNT_FIELDS = {"net_amount", "tax_amount", "gross_amount"}


def _coerce(key: str, value: Any) -> Any:
    if value is None or value == "":
        return None
    try:
        if key in _DATE_FIELDS:
            return date.fromisoformat(str(value))
        if key in _AMOUNT_FIELDS:
            amount = Decimal(str(value)).quantize(Decimal("0.01"))
            if abs(amount) >= Decimal("1e12"):
                raise ValueError
            return amount
        if key == "document_type":
            return DocumentType(value)
    except (ValueError, ArithmeticError) as exc:
        raise ReviewError(f"Ungültiger Wert für {key}: {value!r}") from exc
    if not isinstance(value, str) or len(value) > 500:
        raise ReviewError(f"Ungültiger Wert für {key}")
    return value.strip()


def _apply_corrections(meta: InvoiceMetadata, corrections: dict[str, Any]) -> dict[str, Any]:
    applied: dict[str, Any] = {}
    sources = dict(meta.field_sources or {})
    for key, value in corrections.items():
        if key not in CORRECTABLE:
            raise ReviewError(f"Feld {key} ist nicht korrigierbar")
        value = _coerce(key, value)
        if key == "supplier_vat_id" and value is not None:
            value = normalize_vat_id(value)
            if value is None:
                raise ReviewError("Ungültige USt-ID")
        if key == "currency" and value is not None:
            value = str(value).upper()
        old = getattr(meta, key)
        setattr(meta, key, value)
        sources[key] = "manual"
        applied[key] = {"old": str(old) if isinstance(old, (Decimal, date)) else getattr(old, "value", old),
                        "new": str(value) if isinstance(value, (Decimal, date)) else getattr(value, "value", value)}
    meta.field_sources = sources
    return applied


def review_document(db: Session, doc_id: int, actor: Actor, *, decision: str, reason: str,
                    corrections: dict[str, Any] | None = None, duplicate_of: int | None = None,
                    allowed_company_ids: set[int] | None = None) -> Document:
    doc = db.scalar(select(Document).where(Document.id == doc_id).with_for_update())
    if doc is None or (allowed_company_ids is not None and doc.company_id not in allowed_company_ids):
        raise ReviewError("Nicht gefunden", 404)
    if doc.status not in REVIEWABLE:
        raise ReviewError(f"Dokument im Status {doc.status.value} kann nicht geprüft werden", 409)
    previous = doc.status
    now = datetime.now(UTC)
    details: dict[str, Any] = {"decision": decision, "reason": reason, "previous_status": previous.value}

    if decision == "ignore":
        doc.status = DocumentStatus.ignored
    elif decision == "mark_duplicate":
        other = db.get(Document, duplicate_of) if duplicate_of else None
        if other is None or other.company_id != doc.company_id or other.id == doc.id:
            raise ReviewError("Bezugsdokument fehlt oder gehört zu einem anderen Unternehmen")
        doc.status = DocumentStatus.duplicate
        db.execute(_upsert_dup(doc, other.id, DuplicateDecision.confirmed_duplicate, actor, now))
        details["duplicate_of"] = other.id
    elif decision == "approve":
        meta = db.scalar(select(InvoiceMetadata).where(InvoiceMetadata.document_id == doc.id))
        if meta is None:
            meta = InvoiceMetadata(company_id=doc.company_id, document_id=doc.id, document_type=DocumentType.unclear,
                                   extraction_method=ClassificationMethod.manual, field_sources={}, raw_extraction={})
            db.add(meta)
        if corrections:
            details["corrections"] = _apply_corrections(meta, corrections)
        if meta.document_type not in INVOICE_LIKE:
            raise ReviewError("Freigabe nur für Rechnung, Gutschrift oder Storno – Dokumentart korrigieren")
        inv = _to_invoice(meta)
        missing = inv.missing_required()
        if missing:
            raise ReviewError("Pflichtangaben fehlen: " + ", ".join(missing))
        meta.invoice_number_normalized = normalize_invoice_number(meta.invoice_number)
        meta.supplier_key = supplier_key(inv)
        meta.identity_hash = identity_hash(doc.company_id, inv)
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                   {"k": f"inv:{doc.company_id}:{meta.supplier_key}:{meta.invoice_number_normalized}"})
        clash = db.scalar(
            select(Document.id).join(InvoiceMetadata, InvoiceMetadata.document_id == Document.id)
            .where(InvoiceMetadata.company_id == doc.company_id, InvoiceMetadata.identity_hash == meta.identity_hash,
                   Document.id != doc.id, Document.status.in_(LIVE)).limit(1))
        if clash is not None:
            raise ReviewError(f"Gleiche Rechnung ist bereits als Dokument {clash} freigegeben", 409)
        doc.document_type = meta.document_type
        doc.status = (DocumentStatus.awaiting_datev_xml_integration
                      if doc.source_kind in (SourceKind.xml_xrechnung, SourceKind.xml_other)
                      else DocumentStatus.ready_for_datev)
        db.execute(update(InvoiceDuplicate)
                   .where(InvoiceDuplicate.document_id == doc.id, InvoiceDuplicate.decision == DuplicateDecision.needs_review)
                   .values(decision=DuplicateDecision.not_duplicate, decided_by=actor.user_id, decided_at=now))
    else:
        raise ReviewError(f"Unbekannte Entscheidung: {decision}")

    doc.review_reason = f"Manuell ({decision}): {reason}"[:4000]
    details["new_status"] = doc.status.value
    audit(db, actor, f"document.review.{decision}", company_id=doc.company_id, entity_type="document",
          entity_id=doc.id, details=details)
    return doc


def _upsert_dup(doc: Document, other_id: int, decision: DuplicateDecision, actor: Actor, now: datetime):
    stmt = insert(InvoiceDuplicate).values(
        company_id=doc.company_id, document_id=doc.id, duplicate_of_document_id=other_id,
        match_level=DuplicateMatchLevel.possible, decision=decision, decided_by=actor.user_id, decided_at=now,
        details={"manual": True},
    )
    return stmt.on_conflict_do_update(constraint="uq_invoice_duplicates_pair",
                                      set_={"decision": decision, "decided_by": actor.user_id, "decided_at": now})
