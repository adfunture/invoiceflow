"""Stammdaten für SBC symbio und Adfunture Outdoor.

Idempotent: legt nur fehlende Datensätze an und überschreibt nie Werte, die
im Dashboard geändert wurden. Postfächer starten deaktiviert, DATEV-Ziele
unverifiziert, automatischer Versand aus.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from invoiceflow.db.enums import DatevChannel, EmailProvider, TlsMode
from invoiceflow.db.models import Company, DatevDestination, Mailbox
from invoiceflow.services import app_settings
from invoiceflow.services.audit import Actor, audit
from invoiceflow.services.mailboxes import DEFAULT_FOLDER, ensure_folder


@dataclass(frozen=True)
class MailboxSeed:
    email: str
    credential_ref: str
    imap_host: str | None
    imap_username: str | None
    # False = Domain muss vor Aktivierung im Dashboard bestätigt werden.
    domain_preverified: bool


@dataclass(frozen=True)
class DatevSeed:
    target_address: str  # unverifiziert!
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_tls_mode: TlsMode = TlsMode.ssl


@dataclass(frozen=True)
class CompanySeed:
    slug: str
    legal_name: str
    short_name: str
    provider: EmailProvider
    recipient_aliases: list[str]
    mailboxes: list[MailboxSeed] = field(default_factory=list)
    datev: DatevSeed | None = None


COMPANIES: list[CompanySeed] = [
    CompanySeed(
        slug="sbc",
        legal_name="SBC symbio GmbH & Co. KG",
        short_name="SBC symbio",
        provider=EmailProvider.all_inkl,
        recipient_aliases=["SBC symbio GmbH & Co. KG", "SBC symbio GmbH", "SBC symbio", "sbc-symbio.de"],
        mailboxes=[
            # ALL-INKL: IMAP-Host (w0xxxxxx.kasserver.com) und Login stehen im KAS je Postfach – noch offen.
            MailboxSeed("mail@sbc-symbio.de", "INVOICEFLOW_SECRET_IMAP_SBC_MAIL", None, None, domain_preverified=False),
            MailboxSeed("sascha.senninger@sbc-symbio.de", "INVOICEFLOW_SECRET_IMAP_SBC_SENNINGER", None, None, domain_preverified=False),
            MailboxSeed("invoice@sbc-symbio.de", "INVOICEFLOW_SECRET_IMAP_SBC_INVOICE", None, None, domain_preverified=True),
        ],
        # Aus Screenshot übernommen, Format auffällig (siehe docs/IMPLEMENTATION_PLAN.md).
        datev=DatevSeed("5462c653-86b9-4c24-b95a-8962c-f4235f1@uploadmail.datev.de"),
    ),
    CompanySeed(
        slug="adfunture",
        legal_name="Adfunture Outdoor GmbH",
        short_name="Adfunture",
        provider=EmailProvider.strato,
        recipient_aliases=["Adfunture Outdoor GmbH", "Adfunture Outdoor", "Adfunture", "adfunture.de"],
        mailboxes=[
            MailboxSeed("info@adfunture.de", "INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO", "imap.strato.de", "info@adfunture.de", True),
            MailboxSeed("invoice@adfunture.de", "INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INVOICE", "imap.strato.de", "invoice@adfunture.de", True),
        ],
        datev=DatevSeed(
            "90f44e89-d150-453a-a050-ea0-fe82180f6@uploadmail.datev.de",
            smtp_host="smtp.strato.de",
            smtp_port=465,
        ),
    ),
]


def seed(db: Session) -> dict[str, int]:
    actor = Actor.system("seed")
    created = {"companies": 0, "mailboxes": 0, "datev_destinations": 0}
    app_settings.ensure_defaults(db)

    for c in COMPANIES:
        company = db.scalar(select(Company).where(Company.slug == c.slug))
        if company is None:
            company = Company(
                slug=c.slug,
                legal_name=c.legal_name,
                short_name=c.short_name,
                email_provider=c.provider,
                recipient_aliases=c.recipient_aliases,
            )
            db.add(company)
            db.flush()
            created["companies"] += 1
            audit(db, actor, "company.created", company_id=company.id, entity_type="company", entity_id=company.id)

        for m in c.mailboxes:
            mailbox = db.scalar(select(Mailbox).where(Mailbox.email_address == m.email))
            if mailbox is None:
                mailbox = Mailbox(
                    company_id=company.id,
                    email_address=m.email,
                    provider=c.provider,
                    imap_host=m.imap_host,
                    imap_port=993,
                    imap_tls_mode=TlsMode.ssl,
                    imap_username=m.imap_username,
                    credential_ref=m.credential_ref,
                    is_active=False,
                    domain_verified_at=datetime.now(UTC) if m.domain_preverified else None,
                )
                db.add(mailbox)
                db.flush()
                created["mailboxes"] += 1
                audit(db, actor, "mailbox.created", company_id=company.id, entity_type="mailbox", entity_id=mailbox.id,
                      details={"email": m.email, "domain_preverified": m.domain_preverified})
            elif mailbox.company_id != company.id:
                raise RuntimeError(f"Postfach {m.email} ist einem anderen Unternehmen zugeordnet – Seed abgebrochen")
            ensure_folder(db, mailbox, DEFAULT_FOLDER, actor)

        if c.datev is not None:
            dest = db.scalar(
                select(DatevDestination).where(
                    DatevDestination.company_id == company.id, DatevDestination.channel == DatevChannel.upload_mail
                )
            )
            if dest is None:
                dest = DatevDestination(
                    company_id=company.id,
                    channel=DatevChannel.upload_mail,
                    target_address=c.datev.target_address.lower(),
                    smtp_host=c.datev.smtp_host,
                    smtp_port=c.datev.smtp_port,
                    smtp_tls_mode=c.datev.smtp_tls_mode,
                    auto_send_enabled=False,
                    notes="Adresse aus Screenshot übernommen – vor Produktivbetrieb in DATEV Unternehmen online prüfen.",
                )
                db.add(dest)
                db.flush()
                created["datev_destinations"] += 1
                audit(db, actor, "datev.destination_created", company_id=company.id, entity_type="datev_destination",
                      entity_id=dest.id, details={"address": dest.target_address, "verified": False})
    return created
