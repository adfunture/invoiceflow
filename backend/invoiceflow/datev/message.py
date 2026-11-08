"""Aufbau der Mail an DATEV Upload Mail: ein Beleg je Mail, Original unverändert als Anhang."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

from invoiceflow.db.models import Document, InvoiceMetadata

ATTACHMENT_TYPES = {
    "application/pdf": ("application", "pdf", ".pdf"),
    "image/jpeg": ("image", "jpeg", ".jpg"),
    "image/png": ("image", "png", ".png"),
    "image/tiff": ("image", "tiff", ".tif"),
}


class MessageError(ValueError):
    pass


def attachment_filename(doc: Document) -> str:
    _, _, ext = ATTACHMENT_TYPES[doc.mime_type]
    name = doc.original_filename or f"beleg-{doc.id}"
    name = re.sub(r"[^\w.\- ()äöüÄÖÜß]", "_", name).strip(" .")[:150] or f"beleg-{doc.id}"
    if not name.lower().endswith(ext) and not (ext == ".jpg" and name.lower().endswith(".jpeg")):
        name += ext
    return name


def build_message(
    *,
    doc: Document,
    meta: InvoiceMetadata | None,
    data: bytes,
    sender: str,
    recipient: str,
    transmission_id: int,
) -> EmailMessage:
    if doc.mime_type not in ATTACHMENT_TYPES:
        raise MessageError(f"Dateityp {doc.mime_type} wird nicht per Upload Mail gesendet")
    maintype, subtype, _ = ATTACHMENT_TYPES[doc.mime_type]
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    label = " ".join(p for p in [
        meta.supplier_name if meta else None, meta.invoice_number if meta else None,
    ] if p) or f"Beleg {doc.id}"
    msg["Subject"] = f"Beleg: {label}"[:200]
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg["Message-ID"] = make_msgid(idstring=f"invoiceflow-{transmission_id}", domain=sender.rsplit("@", 1)[-1])
    msg["X-InvoiceFlow-Transmission"] = str(transmission_id)
    msg["X-InvoiceFlow-Document-SHA256"] = doc.sha256
    msg.set_content(
        "Beleg automatisch übermittelt durch InvoiceFlow.\n"
        f"Dokument-ID: {doc.id}\nSHA-256: {doc.sha256}\n"
    )
    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=attachment_filename(doc))
    return msg
