"""SQLAlchemy-Modelle.

Mandantentrennung: Jede geschäftliche Tabelle hat ``company_id``. Verweise
zwischen geschäftlichen Tabellen laufen über zusammengesetzte Fremdschlüssel
``(company_id, <id>)`` auf ``UNIQUE (company_id, id)`` der Zieltabelle. Damit
lehnt PostgreSQL jede Zeile ab, die zwei Unternehmen miteinander verknüpfen
würde – unabhängig davon, was der Anwendungscode tut.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from invoiceflow.db import enums as e

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    pass


Base.metadata.naming_convention = NAMING_CONVENTION


def enum_col(enum_cls: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=40,
        validate_strings=True,
        values_callable=lambda cls: [m.value for m in cls],
    )


def created_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def updated_at_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


def pk_col() -> Mapped[int]:
    return mapped_column(BigInteger, primary_key=True, autoincrement=True)


def company_fk(table: str, column: str, *, nullable: bool = False, ondelete: str = "RESTRICT") -> ForeignKeyConstraint:
    """Zusammengesetzter FK (company_id, column) -> table(company_id, id)."""
    return ForeignKeyConstraint(
        ["company_id", column],
        [f"{table}.company_id", f"{table}.id"],
        ondelete=ondelete,
        name=f"fk_{column}_same_company",
    )


# ---------------------------------------------------------------------------
# Benutzer & Zugriff
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("email = lower(email)", name="email_lowercase"),)

    id: Mapped[int] = pk_col()
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[e.UserRole] = mapped_column(enum_col(e.UserRole, "user_role"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class UserCompanyAccess(Base):
    """Nicht-Admins sehen nur Unternehmen, die hier eingetragen sind."""

    __tablename__ = "user_company_access"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = pk_col()
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(500))


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    __table_args__ = (
        Index("ix_login_attempts_email_time", "email", "attempted_at"),
        Index("ix_login_attempts_ip_time", "ip_address", "attempted_at"),
    )

    id: Mapped[int] = pk_col()
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(INET)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ---------------------------------------------------------------------------
# Unternehmen & Postfächer
# ---------------------------------------------------------------------------


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = pk_col()
    slug: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    legal_name: Mapped[str] = mapped_column(String(300), nullable=False)
    short_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email_provider: Mapped[e.EmailProvider] = mapped_column(enum_col(e.EmailProvider, "email_provider"), nullable=False)
    vat_id: Mapped[str | None] = mapped_column(String(20))
    # Namensvarianten, an denen erkannt wird, an welches Unternehmen eine Rechnung adressiert ist.
    recipient_aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Mailbox(Base):
    __tablename__ = "mailboxes"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_mailboxes_company_id_id"),
        CheckConstraint("email_address = lower(email_address)", name="email_lowercase"),
        CheckConstraint("imap_port BETWEEN 1 AND 65535", name="imap_port_range"),
        CheckConstraint("credential_ref ~ '^INVOICEFLOW_SECRET_[A-Z0-9_]+$'", name="credential_ref_format"),
        # Ein Postfach kann nur aktiv sein, wenn seine Domain bestätigt wurde.
        CheckConstraint("NOT is_active OR domain_verified_at IS NOT NULL", name="active_requires_domain"),
    )

    id: Mapped[int] = pk_col()
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False, index=True)
    email_address: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    provider: Mapped[e.EmailProvider] = mapped_column(enum_col(e.EmailProvider, "mailbox_provider"), nullable=False)
    imap_host: Mapped[str | None] = mapped_column(String(255))  # None = noch nicht konfiguriert
    imap_port: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("993"))
    imap_tls_mode: Mapped[e.TlsMode] = mapped_column(enum_col(e.TlsMode, "imap_tls_mode"), nullable=False, server_default=e.TlsMode.ssl.value)
    imap_username: Mapped[str | None] = mapped_column(String(320))
    credential_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    domain_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    domain_verified_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Lease: höchstens ein Worker ruft ein Postfach gleichzeitig ab. Läuft ab, wenn der Worker stirbt.
    sync_locked_by: Mapped[str | None] = mapped_column(String(200))
    sync_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class MailboxSyncState(Base):
    """Fortschritt je Ordner. UIDs sind nur innerhalb einer UIDVALIDITY gültig."""

    __tablename__ = "mailbox_sync_states"
    __table_args__ = (
        UniqueConstraint("mailbox_id", "folder_name", name="uq_mailbox_sync_states_mailbox_folder"),
        UniqueConstraint("company_id", "id", name="uq_mailbox_sync_states_company_id_id"),
        company_fk("mailboxes", "mailbox_id", ondelete="CASCADE"),
    )

    id: Mapped[int] = pk_col()
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    mailbox_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    folder_name: Mapped[str] = mapped_column(String(500), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger)
    last_seen_uid: Mapped[int | None] = mapped_column(BigInteger)
    backfill_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


# ---------------------------------------------------------------------------
# E-Mails & Anhänge
# ---------------------------------------------------------------------------


class EmailMessage(Base):
    __tablename__ = "email_messages"
    __table_args__ = (
        UniqueConstraint("mailbox_id", "folder_name", "uidvalidity", "uid", name="uq_email_messages_imap_identity"),
        UniqueConstraint("company_id", "id", name="uq_email_messages_company_id_id"),
        company_fk("mailboxes", "mailbox_id"),
        company_fk("mailbox_sync_states", "sync_state_id"),
        Index("ix_email_messages_company_received", "company_id", "received_at"),
        Index("ix_email_messages_message_id_header", "company_id", "message_id_header"),
        Index("ix_email_messages_mailbox_raw_sha", "mailbox_id", "raw_sha256"),
    )

    id: Mapped[int] = pk_col()
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    mailbox_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sync_state_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    folder_name: Mapped[str] = mapped_column(String(500), nullable=False)
    uidvalidity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uid: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id_header: Mapped[str | None] = mapped_column(String(998))
    subject: Mapped[str | None] = mapped_column(Text)
    from_address: Mapped[str | None] = mapped_column(String(998))
    to_addresses: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # IMAP INTERNALDATE
    header_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # None, wenn die Nachricht das Größenlimit überschreitet und nicht heruntergeladen wurde.
    raw_sha256: Mapped[str | None] = mapped_column(String(64))
    raw_storage_key: Mapped[str | None] = mapped_column(String(500))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    body_preview: Mapped[str | None] = mapped_column(Text)
    body_mentions_invoice: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Rohnachricht nach Ablauf der Aufbewahrungsfrist entfernt (nur E-Mails ohne Rechnungsbezug).
    raw_purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attachment_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    detected_invoice_links: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    status: Mapped[e.EmailMessageStatus] = mapped_column(
        enum_col(e.EmailMessageStatus, "email_message_status"), nullable=False, server_default=e.EmailMessageStatus.fetched.value
    )
    review_reason: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class EmailAttachment(Base):
    __tablename__ = "email_attachments"
    __table_args__ = (
        UniqueConstraint("email_message_id", "part_index", name="uq_email_attachments_message_part"),
        UniqueConstraint("company_id", "id", name="uq_email_attachments_company_id_id"),
        company_fk("email_messages", "email_message_id", ondelete="CASCADE"),
        Index("ix_email_attachments_company_sha", "company_id", "sha256"),
    )

    id: Mapped[int] = pk_col()
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    email_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    part_index: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str | None] = mapped_column(String(1000))
    declared_content_type: Mapped[str | None] = mapped_column(String(255))
    detected_content_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    storage_key: Mapped[str | None] = mapped_column(String(500))
    is_inline: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    content_id: Mapped[str | None] = mapped_column(String(998))
    safety_status: Mapped[e.AttachmentSafety] = mapped_column(enum_col(e.AttachmentSafety, "attachment_safety"), nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()


# ---------------------------------------------------------------------------
# Dokumente & Rechnungsdaten
# ---------------------------------------------------------------------------


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_documents_company_id_id"),
        UniqueConstraint("attachment_id", name="uq_documents_attachment"),
        company_fk("email_messages", "email_message_id"),
        company_fk("email_attachments", "attachment_id"),
        CheckConstraint("ai_confidence IS NULL OR (ai_confidence >= 0 AND ai_confidence <= 1)", name="ai_confidence_range"),
        Index("ix_documents_company_sha", "company_id", "sha256"),
        Index("ix_documents_company_status", "company_id", "status"),
    )

    id: Mapped[int] = pk_col()
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    email_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attachment_id: Mapped[int | None] = mapped_column(BigInteger)  # None bei Rechnung im E-Mail-Text
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(1000))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    source_kind: Mapped[e.SourceKind] = mapped_column(enum_col(e.SourceKind, "source_kind"), nullable=False)
    document_type: Mapped[e.DocumentType] = mapped_column(
        enum_col(e.DocumentType, "document_type"), nullable=False, server_default=e.DocumentType.unclear.value
    )
    classification_method: Mapped[e.ClassificationMethod | None] = mapped_column(enum_col(e.ClassificationMethod, "classification_method"))
    ai_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    status: Mapped[e.DocumentStatus] = mapped_column(
        enum_col(e.DocumentStatus, "document_status"), nullable=False, server_default=e.DocumentStatus.pending.value
    )
    review_reason: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    embedded_xml_storage_key: Mapped[str | None] = mapped_column(String(500))
    embedded_xml_filename: Mapped[str | None] = mapped_column(String(500))
    page_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class InvoiceMetadata(Base):
    __tablename__ = "invoice_metadata"
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_invoice_metadata_document"),
        company_fk("documents", "document_id", ondelete="CASCADE"),
        CheckConstraint("currency IS NULL OR currency ~ '^[A-Z]{3}$'", name="currency_iso"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"),
        Index("ix_invoice_metadata_identity", "company_id", "identity_hash"),
        Index("ix_invoice_metadata_supplier_number", "company_id", "supplier_key", "invoice_number_normalized"),
    )

    id: Mapped[int] = pk_col()
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_type: Mapped[e.DocumentType] = mapped_column(enum_col(e.DocumentType, "invoice_document_type"), nullable=False)
    invoice_number: Mapped[str | None] = mapped_column(String(200))
    invoice_number_normalized: Mapped[str | None] = mapped_column(String(200))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    service_date: Mapped[date | None] = mapped_column(Date)
    service_period_end: Mapped[date | None] = mapped_column(Date)
    supplier_name: Mapped[str | None] = mapped_column(String(500))
    supplier_address: Mapped[str | None] = mapped_column(Text)
    supplier_vat_id: Mapped[str | None] = mapped_column(String(20))
    supplier_tax_number: Mapped[str | None] = mapped_column(String(40))
    supplier_key: Mapped[str | None] = mapped_column(String(200))  # USt-ID oder normalisierter Name
    net_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    tax_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    gross_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    recipient_name: Mapped[str | None] = mapped_column(String(500))
    recipient_text: Mapped[str | None] = mapped_column(Text)
    iban: Mapped[str | None] = mapped_column(String(34))
    payment_terms: Mapped[str | None] = mapped_column(Text)
    due_date: Mapped[date | None] = mapped_column(Date)
    referenced_invoice_number: Mapped[str | None] = mapped_column(String(200))  # bei Gutschrift/Storno/Korrektur
    extraction_method: Mapped[e.ClassificationMethod] = mapped_column(enum_col(e.ClassificationMethod, "extraction_method"), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    identity_hash: Mapped[str | None] = mapped_column(String(64))
    field_sources: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    raw_extraction: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class InvoiceDuplicate(Base):
    __tablename__ = "invoice_duplicates"
    __table_args__ = (
        UniqueConstraint("document_id", "duplicate_of_document_id", name="uq_invoice_duplicates_pair"),
        CheckConstraint("document_id <> duplicate_of_document_id", name="not_self"),
        company_fk("documents", "document_id", ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["company_id", "duplicate_of_document_id"],
            ["documents.company_id", "documents.id"],
            ondelete="CASCADE",
            name="fk_duplicate_of_document_id_same_company",
        ),
    )

    id: Mapped[int] = pk_col()
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duplicate_of_document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    match_level: Mapped[e.DuplicateMatchLevel] = mapped_column(enum_col(e.DuplicateMatchLevel, "duplicate_match_level"), nullable=False)
    decision: Mapped[e.DuplicateDecision] = mapped_column(enum_col(e.DuplicateDecision, "duplicate_decision"), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


# ---------------------------------------------------------------------------
# DATEV
# ---------------------------------------------------------------------------

_GATE_STEPS = (
    "address_verified_at",
    "sender_authorized_at",
    "assignment_tested_at",
    "duplicate_check_tested_at",
    "test_delivery_confirmed_at",
)


class DatevDestination(Base):
    """DATEV-Ziel je Unternehmen und Kanal inklusive Produktivfreigabe-Checkliste."""

    __tablename__ = "datev_destinations"
    __table_args__ = (
        UniqueConstraint("company_id", "channel", name="uq_datev_destinations_company_channel"),
        UniqueConstraint("company_id", "id", name="uq_datev_destinations_company_id_id"),
        CheckConstraint("target_address IS NULL OR target_address = lower(target_address)", name="target_lowercase"),
        CheckConstraint(
            "channel <> 'upload_mail' OR target_address IS NULL OR target_address LIKE '%@uploadmail.datev.de'",
            name="upload_mail_domain",
        ),
        CheckConstraint(
            "smtp_credential_ref IS NULL OR smtp_credential_ref ~ '^INVOICEFLOW_SECRET_[A-Z0-9_]+$'",
            name="smtp_credential_ref_format",
        ),
        CheckConstraint(
            "production_released_at IS NULL OR (" + " AND ".join(f"{c} IS NOT NULL" for c in _GATE_STEPS) + ")",
            name="release_requires_all_steps",
        ),
        CheckConstraint("NOT auto_send_enabled OR production_released_at IS NOT NULL", name="auto_send_requires_release"),
    )

    id: Mapped[int] = pk_col()
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    channel: Mapped[e.DatevChannel] = mapped_column(enum_col(e.DatevChannel, "datev_channel"), nullable=False)
    target_address: Mapped[str | None] = mapped_column(String(320))
    sender_address: Mapped[str | None] = mapped_column(String(320))
    smtp_host: Mapped[str | None] = mapped_column(String(255))
    smtp_port: Mapped[int | None] = mapped_column(Integer)
    smtp_tls_mode: Mapped[e.TlsMode] = mapped_column(enum_col(e.TlsMode, "smtp_tls_mode"), nullable=False, server_default=e.TlsMode.ssl.value)
    smtp_username: Mapped[str | None] = mapped_column(String(320))
    smtp_credential_ref: Mapped[str | None] = mapped_column(String(120))

    address_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    address_verified_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    sender_authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sender_authorized_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    assignment_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assignment_tested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    duplicate_check_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duplicate_check_tested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    test_delivery_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    test_delivery_confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    production_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    production_released_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    auto_send_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class DatevTransmission(Base):
    """Ein Versand je Dokument. Die partiellen Unique-Indizes sind die letzte
    Verteidigungslinie gegen Doppelversand – auch bei parallelen Workern."""

    __tablename__ = "datev_transmissions"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_datev_transmissions_company_id_id"),
        company_fk("documents", "document_id"),
        company_fk("datev_destinations", "destination_id"),
        Index(
            "uq_datev_transmissions_document_live",
            "document_id",
            unique=True,
            postgresql_where=text("NOT test_mode"),
        ),
        Index(
            "uq_datev_transmissions_file_live",
            "company_id",
            "file_sha256",
            unique=True,
            postgresql_where=text("NOT test_mode AND override_by IS NULL"),
        ),
        Index(
            "uq_datev_transmissions_identity_live",
            "company_id",
            "identity_hash",
            unique=True,
            postgresql_where=text("NOT test_mode AND override_by IS NULL AND identity_hash IS NOT NULL"),
        ),
        Index("ix_datev_transmissions_company_status", "company_id", "status"),
        CheckConstraint("override_by IS NULL OR override_reason IS NOT NULL", name="override_needs_reason"),
        CheckConstraint("send_mode IN ('automatic', 'manual', 'test_delivery', 'dry_run')", name="send_mode_valid"),
        CheckConstraint("send_mode <> 'dry_run' OR test_mode", name="dry_run_is_test_mode"),
        Index("ix_datev_transmissions_due", "status", "next_attempt_at"),
    )

    id: Mapped[int] = pk_col()
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    document_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    destination_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel: Mapped[e.DatevChannel] = mapped_column(enum_col(e.DatevChannel, "transmission_channel"), nullable=False)
    destination_address: Mapped[str | None] = mapped_column(String(320))  # Schnappschuss zum Versandzeitpunkt
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[e.TransmissionStatus] = mapped_column(
        enum_col(e.TransmissionStatus, "transmission_status"), nullable=False, server_default=e.TransmissionStatus.pending.value
    )
    test_mode: Mapped[bool] = mapped_column(Boolean, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sending_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    smtp_message_id: Mapped[str | None] = mapped_column(String(998))
    last_smtp_response: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    override_reason: Mapped[str | None] = mapped_column(Text)
    override_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    override_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # automatic: nur nach Produktivfreigabe + Auto-Versand; manual: von Prüfer ausgelöst;
    # test_delivery: Testlieferung vor der Produktivfreigabe (nur Admin); dry_run: Testmodus, nichts gesendet.
    send_mode: Mapped[str] = mapped_column(String(20), nullable=False, server_default="automatic")
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class DatevTransmissionAttempt(Base):
    __tablename__ = "datev_transmission_attempts"
    __table_args__ = (
        UniqueConstraint("transmission_id", "attempt_no", name="uq_datev_attempts_transmission_no"),
        company_fk("datev_transmissions", "transmission_id", ondelete="CASCADE"),
    )

    id: Mapped[int] = pk_col()
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False)
    transmission_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    destination_address: Mapped[str | None] = mapped_column(String(320))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[e.AttemptOutcome | None] = mapped_column(enum_col(e.AttemptOutcome, "attempt_outcome"))
    smtp_response_code: Mapped[int | None] = mapped_column(Integer)
    smtp_response: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    test_mode: Mapped[bool] = mapped_column(Boolean, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(200))


# ---------------------------------------------------------------------------
# Jobs, Audit, Einstellungen
# ---------------------------------------------------------------------------


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_processing_jobs_dedup_key"),
        CheckConstraint("attempts >= 0 AND attempts <= max_attempts + 1", name="attempts_range"),
        Index("ix_processing_jobs_pick", "job_type", "status", "run_after", "priority"),
        Index("ix_processing_jobs_lease", "status", "lease_expires_at"),
    )

    id: Mapped[int] = pk_col()
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), nullable=False, index=True)
    job_type: Mapped[e.JobType] = mapped_column(enum_col(e.JobType, "job_type"), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(300), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[e.JobStatus] = mapped_column(enum_col(e.JobStatus, "job_status"), nullable=False, server_default=e.JobStatus.queued.value)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(200))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class AuditLog(Base):
    """Append-only; UPDATE/DELETE werden per Trigger abgewiesen (siehe Migration)."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_company_time", "company_id", "occurred_at"),
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = pk_col()
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    actor_type: Mapped[e.ActorType] = mapped_column(enum_col(e.ActorType, "actor_type"), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_label: Mapped[str | None] = mapped_column(String(320))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(100))
    entity_id: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    ip_address: Mapped[str | None] = mapped_column(INET)


class ApplicationSetting(Base):
    __tablename__ = "application_settings"
    __table_args__ = (
        UniqueConstraint("company_id", "key", name="uq_application_settings_company_key", postgresql_nulls_not_distinct=True),
    )

    id: Mapped[int] = pk_col()
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = updated_at_col()
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class WorkerHeartbeat(Base):
    """Lebenszeichen der Hintergrund-Worker (für Dashboard und Monitoring)."""

    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    worker_type: Mapped[str] = mapped_column(String(50), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class AlertState(Base):
    """Zustand je Überwachungsregel: verhindert Alarmfluten, meldet Entwarnung."""

    __tablename__ = "alert_states"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
