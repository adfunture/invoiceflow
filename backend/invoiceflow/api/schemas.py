"""Pydantic-Schemas der API. Eingabeschemas verbieten unbekannte Felder."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from invoiceflow.db.enums import ActorType, DatevChannel, EmailProvider, TlsMode, UserRole
from invoiceflow.security.secrets import validate_credential_ref


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Output(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Auth -------------------------------------------------------------------


class LoginIn(StrictInput):
    # Bewusst keine strenge Adressprüfung: Ein Login mit unbekannter Adresse ist einfach "falsch",
    # interne Domains (z. B. *.local) sollen funktionieren.
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    password: str = Field(min_length=1, max_length=1024)


class ChangePasswordIn(StrictInput):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


class UserOut(Output):
    id: int
    email: str
    display_name: str
    role: UserRole
    must_change_password: bool


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str
    company_ids: list[int] | None


# --- Unternehmen --------------------------------------------------------------


class CompanyOut(Output):
    id: int
    slug: str
    legal_name: str
    short_name: str
    email_provider: EmailProvider
    vat_id: str | None
    recipient_aliases: list[str]
    is_active: bool


class CompanyUpdateIn(StrictInput):
    vat_id: str | None = Field(default=None, pattern=r"^[A-Z]{2}[A-Z0-9]{2,12}$")
    recipient_aliases: list[str] | None = Field(default=None, max_length=30)

    @field_validator("recipient_aliases")
    @classmethod
    def _aliases(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        cleaned = [a.strip() for a in v if a and a.strip()]
        if not cleaned:
            raise ValueError("mindestens ein Alias")
        if any(len(a) > 200 for a in cleaned):
            raise ValueError("Alias zu lang")
        return cleaned


# --- Postfächer ---------------------------------------------------------------


class FolderOut(Output):
    id: int
    folder_name: str
    is_enabled: bool
    uidvalidity: int | None
    last_seen_uid: int | None
    backfill_completed_at: datetime | None
    last_synced_at: datetime | None
    last_error: str | None
    exclusion_reason: str | None = None
    discovered_at: datetime | None = None
    missing_since: datetime | None = None


class ConnectionTestOut(BaseModel):
    ok: bool
    error: str | None = None
    folders: list[str] = []
    inbox_uidvalidity: int | None = None
    inbox_messages: int | None = None


class MailboxOut(Output):
    id: int
    company_id: int
    email_address: str
    display_name: str | None
    provider: EmailProvider
    imap_host: str | None
    imap_port: int
    imap_tls_mode: TlsMode
    imap_username: str | None
    credential_ref: str
    credential_configured: bool = False
    is_active: bool
    domain_verified_at: datetime | None
    last_successful_sync_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    consecutive_failures: int
    last_sync_started_at: datetime | None = None
    sync_requested_at: datetime | None = None
    activation_blockers: list[str] = []
    folders: list[FolderOut] = []


_HOST_PATTERN = r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$"


class MailboxCreateIn(StrictInput):
    company_id: int
    email_address: EmailStr
    display_name: str | None = Field(default=None, max_length=200)
    provider: EmailProvider
    imap_host: str | None = Field(default=None, pattern=_HOST_PATTERN)
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_tls_mode: TlsMode = TlsMode.ssl
    imap_username: str | None = Field(default=None, max_length=320)
    credential_ref: str

    @field_validator("credential_ref")
    @classmethod
    def _ref(cls, v: str) -> str:
        return validate_credential_ref(v)


class MailboxUpdateIn(StrictInput):
    """company_id und email_address sind bewusst nicht änderbar."""

    display_name: str | None = Field(default=None, max_length=200)
    imap_host: str | None = Field(default=None, pattern=_HOST_PATTERN)
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    imap_tls_mode: TlsMode | None = None
    imap_username: str | None = Field(default=None, max_length=320)
    credential_ref: str | None = None

    @field_validator("credential_ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        return None if v is None else validate_credential_ref(v)


class ConfirmIn(StrictInput):
    confirmation: str = Field(min_length=1, max_length=320)


class ActiveIn(StrictInput):
    active: bool


class FolderIn(StrictInput):
    folder_name: str = Field(min_length=1, max_length=500)
    enabled: bool = True


# --- DATEV --------------------------------------------------------------------


class GateStepOut(BaseModel):
    key: str
    label: str
    done_at: datetime | None
    done_by: int | None


class DatevDestinationOut(BaseModel):
    id: int
    company_id: int
    channel: DatevChannel
    target_address: str | None
    address_format_warnings: list[str]
    sender_address: str | None
    smtp_host: str | None
    smtp_port: int | None
    smtp_tls_mode: TlsMode
    smtp_username: str | None
    smtp_credential_ref: str | None
    smtp_credential_configured: bool
    gate_steps: list[GateStepOut]
    production_released_at: datetime | None
    production_released_by: int | None
    auto_send_enabled: bool
    send_allowed: bool
    send_blockers: list[str]
    test_mode: bool
    notes: str | None


class DatevAddressIn(StrictInput):
    target_address: EmailStr


class DatevSenderIn(StrictInput):
    sender_address: EmailStr
    smtp_host: str = Field(pattern=_HOST_PATTERN)
    smtp_port: int = Field(ge=1, le=65535)
    smtp_tls_mode: TlsMode
    smtp_username: str = Field(min_length=1, max_length=320)
    smtp_credential_ref: str

    @field_validator("smtp_credential_ref")
    @classmethod
    def _ref(cls, v: str) -> str:
        return validate_credential_ref(v)


class GateStepIn(StrictInput):
    note: str | None = Field(default=None, max_length=2000)


class ReleaseIn(StrictInput):
    acknowledgement: str


class AutoSendIn(StrictInput):
    enabled: bool


# --- Einstellungen & Audit ----------------------------------------------------


class SettingOut(BaseModel):
    key: str
    value: Any
    default: Any
    description: str
    company_id: int | None


class SettingIn(StrictInput):
    value: Any
    company_id: int | None = None


class AuditOut(Output):
    id: int
    occurred_at: datetime
    company_id: int | None
    actor_type: ActorType
    actor_user_id: int | None
    actor_label: str | None
    action: str
    entity_type: str | None
    entity_id: str | None
    details: dict[str, Any]


class HealthOut(BaseModel):
    status: Literal["ok", "degraded"]
    checks: dict[str, str]


# --- E-Mails & Worker ---------------------------------------------------------


class AttachmentOut(Output):
    id: int
    part_index: int
    filename: str | None
    declared_content_type: str | None
    detected_content_type: str | None
    size_bytes: int
    sha256: str | None
    is_inline: bool
    safety_status: str
    rejection_reason: str | None


class EmailMessageOut(Output):
    id: int
    company_id: int
    mailbox_id: int
    folder_name: str
    uid: int
    message_id_header: str | None
    subject: str | None
    from_address: str | None
    to_addresses: list[str]
    received_at: datetime
    header_date: datetime | None
    size_bytes: int
    attachment_count: int
    detected_invoice_links: list[str]
    body_mentions_invoice: bool
    status: str
    review_reason: str | None
    imported_at: datetime


class EmailMessageDetailOut(EmailMessageOut):
    body_preview: str | None
    raw_sha256: str | None
    attachments: list[AttachmentOut] = []


class WorkerOut(Output):
    worker_id: str
    worker_type: str
    started_at: datetime
    last_beat_at: datetime
    stale: bool = False
    details: dict[str, Any]


# --- Dokumente ------------------------------------------------------------------


class InvoiceMetadataOut(Output):
    document_type: str
    invoice_number: str | None
    invoice_date: date | None
    service_date: date | None
    supplier_name: str | None
    supplier_address: str | None
    supplier_vat_id: str | None
    net_amount: Decimal | None
    tax_amount: Decimal | None
    gross_amount: Decimal | None
    currency: str | None
    recipient_name: str | None
    iban: str | None
    payment_terms: str | None
    due_date: date | None
    referenced_invoice_number: str | None
    extraction_method: str
    confidence: Decimal | None
    field_sources: dict[str, Any]
    raw_extraction: dict[str, Any]


class DocumentOut(Output):
    id: int
    company_id: int
    email_message_id: int
    original_filename: str | None
    mime_type: str | None
    sha256: str
    size_bytes: int
    source_kind: str
    document_type: str
    classification_method: str | None
    ai_confidence: Decimal | None
    status: str
    review_reason: str | None
    page_count: int | None
    has_embedded_xml: bool = False
    created_at: datetime
    updated_at: datetime
    invoice: InvoiceMetadataOut | None = None
    mailbox_email: str | None = None
    received_at: datetime | None = None


class DuplicateOut(Output):
    document_id: int
    duplicate_of_document_id: int
    match_level: str
    decision: str
    details: dict[str, Any]


class DocumentDetailOut(DocumentOut):
    duplicates: list[DuplicateOut] = []
    message_subject: str | None = None
    message_from: str | None = None


class ReviewIn(StrictInput):
    decision: Literal["approve", "ignore", "mark_duplicate"]
    reason: str = Field(min_length=3, max_length=2000)
    duplicate_of_document_id: int | None = None
    corrections: dict[str, Any] | None = None


class BulkIgnoreIn(StrictInput):
    document_ids: list[int] = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=3, max_length=2000)


class BulkFailureOut(BaseModel):
    document_id: int
    error: str


class BulkIgnoreOut(BaseModel):
    ignored: list[int]
    failed: list[BulkFailureOut]


# --- DATEV-Übertragungen --------------------------------------------------------


class AttemptOut(Output):
    attempt_no: int
    destination_address: str | None
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    smtp_response_code: int | None
    smtp_response: str | None
    error_message: str | None
    test_mode: bool
    worker_id: str | None


class TransmissionOut(Output):
    id: int
    company_id: int
    document_id: int
    destination_address: str | None
    file_sha256: str
    status: str
    test_mode: bool
    send_mode: str
    attempts: int
    max_attempts: int
    next_attempt_at: datetime | None
    sending_started_at: datetime | None
    sent_at: datetime | None
    smtp_message_id: str | None
    last_smtp_response: str | None
    last_error: str | None
    confirmed_at: datetime | None
    confirmed_by: int | None
    requested_by: int | None
    requested_at: datetime | None
    created_at: datetime
    # Aus Dokument/Rechnungsdaten, damit die Liste den Absender der Rechnung zeigt.
    supplier_name: str | None = None
    invoice_number: str | None = None
    original_filename: str | None = None


class TransmissionDetailOut(TransmissionOut):
    attempts_log: list[AttemptOut] = []


class SendRequestIn(StrictInput):
    mode: Literal["manual", "test_delivery"]


class NoteIn(StrictInput):
    note: str = Field(min_length=1, max_length=2000)


class ResolveIn(StrictInput):
    was_sent: bool
    reason: str = Field(min_length=1, max_length=2000)


class DryRunOut(BaseModel):
    transmission_id: int
    preview: dict[str, Any]
