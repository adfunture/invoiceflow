"""Statusdefinitionen. Werte werden als VARCHAR mit CHECK-Constraint gespeichert."""
from enum import StrEnum


class EmailProvider(StrEnum):
    all_inkl = "all_inkl"
    strato = "strato"
    other = "other"


class TlsMode(StrEnum):
    ssl = "ssl"  # implizites TLS (IMAPS 993 / SMTPS 465)
    starttls = "starttls"


class UserRole(StrEnum):
    admin = "admin"
    reviewer = "reviewer"
    viewer = "viewer"


class EmailMessageStatus(StrEnum):
    fetched = "fetched"
    processing = "processing"
    processed = "processed"
    no_invoice = "no_invoice"
    manual_review = "manual_review"
    error = "error"


class AttachmentSafety(StrEnum):
    ok = "ok"
    rejected_size = "rejected_size"
    rejected_type = "rejected_type"
    corrupt = "corrupt"
    inline_skipped = "inline_skipped"


class SourceKind(StrEnum):
    pdf = "pdf"
    scan_pdf = "scan_pdf"
    zugferd_pdf = "zugferd_pdf"
    xml_xrechnung = "xml_xrechnung"
    xml_other = "xml_other"
    image = "image"
    email_body = "email_body"
    unknown = "unknown"


class DocumentType(StrEnum):
    invoice = "invoice"
    credit_note = "credit_note"
    cancellation = "cancellation"
    offer = "offer"
    delivery_note = "delivery_note"
    dunning = "dunning"
    order_confirmation = "order_confirmation"
    other = "other"
    unclear = "unclear"


class ClassificationMethod(StrEnum):
    rule = "rule"
    xml = "xml"
    ai = "ai"
    manual = "manual"


class DocumentStatus(StrEnum):
    pending = "pending"
    processing = "processing"
    extracted = "extracted"
    duplicate = "duplicate"
    manual_review = "manual_review"
    awaiting_datev_xml_integration = "awaiting_datev_xml_integration"
    ready_for_datev = "ready_for_datev"
    transmitted = "transmitted"
    ignored = "ignored"
    error = "error"


class DuplicateMatchLevel(StrEnum):
    file_hash = "file_hash"
    invoice_identity = "invoice_identity"
    possible = "possible"


class DuplicateDecision(StrEnum):
    confirmed_duplicate = "confirmed_duplicate"
    needs_review = "needs_review"
    not_duplicate = "not_duplicate"


class DatevChannel(StrEnum):
    upload_mail = "upload_mail"
    api = "api"


class TransmissionStatus(StrEnum):
    pending = "pending"
    ready = "ready"
    sending = "sending"
    sent_unconfirmed = "sent_unconfirmed"
    confirmed = "confirmed"
    failed = "failed"
    manual_review = "manual_review"


class AttemptOutcome(StrEnum):
    accepted = "accepted"  # SMTP 250 – keine DATEV-Bestätigung!
    rejected = "rejected"  # SMTP 5xx
    temporary_failure = "temporary_failure"  # SMTP 4xx / Verbindungsfehler vor DATA
    unknown = "unknown"  # Abbruch nach DATA – Ergebnis unklar
    test_mode = "test_mode"  # nicht gesendet


class JobType(StrEnum):
    imap_fetch = "imap_fetch"
    process_message = "process_message"
    process_document = "process_document"
    datev_send = "datev_send"


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    dead = "dead"


class ActorType(StrEnum):
    user = "user"
    system = "system"
    worker = "worker"
