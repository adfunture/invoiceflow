"""Die Datenbank selbst verhindert Mandantenvermischung und Doppelversand."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from invoiceflow.db.enums import DatevChannel, TransmissionStatus
from invoiceflow.db.models import AuditLog, DatevDestination, DatevTransmission, EmailAttachment
from invoiceflow.services import app_settings

from . import factories as f
from .conftest import company_id


def _destination(db, slug: str) -> DatevDestination:
    return db.scalar(select(DatevDestination).where(DatevDestination.company_id == company_id(slug, db)))


def test_message_cannot_reference_mailbox_of_other_company(db):
    adfunture_box = f.mailbox(db, "info@adfunture.de")
    with pytest.raises(IntegrityError):
        f.message(db, adfunture_box, uid=1, company_id=company_id("sbc", db))


def test_attachment_cannot_cross_company(db):
    msg = f.message(db, f.mailbox(db, "info@adfunture.de"), uid=1)
    db.add(EmailAttachment(company_id=company_id("sbc", db), email_message_id=msg.id, part_index=1, size_bytes=1,
                           safety_status="ok"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_same_imap_message_is_unique(db):
    box = f.mailbox(db, "info@adfunture.de")
    f.message(db, box, uid=42)
    with pytest.raises(IntegrityError):
        f.message(db, box, uid=42)


def test_same_uid_with_new_uidvalidity_is_allowed(db):
    box = f.mailbox(db, "info@adfunture.de")
    f.message(db, box, uid=42, uidvalidity=1)
    f.message(db, box, uid=42, uidvalidity=2)


def test_mailbox_company_cannot_change(db):
    box = f.mailbox(db, "info@adfunture.de")
    box.company_id = company_id("sbc", db)
    with pytest.raises(DBAPIError, match="unveraenderlich"):
        db.flush()


def test_mailbox_cannot_be_active_without_verified_domain(db):
    box = f.mailbox(db, "mail@sbc-symbio.de")
    assert box.domain_verified_at is None
    box.is_active = True
    with pytest.raises(IntegrityError, match="active_requires_domain"):
        db.flush()


def test_credential_ref_must_be_prefixed(db):
    box = f.mailbox(db, "info@adfunture.de")
    box.credential_ref = "DATABASE_URL"
    with pytest.raises(IntegrityError, match="credential_ref_format"):
        db.flush()


def test_audit_log_is_append_only(db):
    log_id = db.scalar(select(AuditLog.id).limit(1))
    with pytest.raises(DBAPIError, match="unveraenderlich"):
        db.execute(text("UPDATE audit_logs SET action = 'manipuliert' WHERE id = :id"), {"id": log_id})
    db.rollback()
    with pytest.raises(DBAPIError, match="unveraenderlich"):
        db.execute(text("DELETE FROM audit_logs WHERE id = :id"), {"id": log_id})


def test_auto_send_requires_production_release(db):
    dest = _destination(db, "adfunture")
    dest.auto_send_enabled = True
    with pytest.raises(IntegrityError, match="auto_send_requires_release"):
        db.flush()


def test_release_requires_all_gate_steps(db):
    dest = _destination(db, "adfunture")
    now = datetime.now(UTC)
    dest.address_verified_at = now
    dest.production_released_at = now
    with pytest.raises(IntegrityError, match="release_requires_all_steps"):
        db.flush()


def test_upload_mail_address_must_be_datev_domain(db):
    dest = _destination(db, "adfunture")
    dest.target_address = "rechnung@angreifer.example"
    with pytest.raises(IntegrityError, match="upload_mail_domain"):
        db.flush()


def _transmission(doc, dest, *, test_mode=False, identity=None):
    return DatevTransmission(
        company_id=doc.company_id, document_id=doc.id, destination_id=dest.id, channel=DatevChannel.upload_mail,
        destination_address=dest.target_address, file_sha256=doc.sha256, identity_hash=identity,
        status=TransmissionStatus.ready, test_mode=test_mode,
    )


def test_same_file_cannot_be_transmitted_twice_per_company(db):
    """Dieselbe PDF über info@ und invoice@ → nur eine Live-Übertragung möglich."""
    dest = _destination(db, "adfunture")
    doc1 = f.document(db, f.message(db, f.mailbox(db, "info@adfunture.de"), uid=1), b"%PDF-1.7 rechnung")
    doc2 = f.document(db, f.message(db, f.mailbox(db, "invoice@adfunture.de"), uid=1), b"%PDF-1.7 rechnung")
    db.add(_transmission(doc1, dest))
    db.flush()
    db.add(_transmission(doc2, dest))
    with pytest.raises(IntegrityError, match="uq_datev_transmissions_file_live"):
        db.flush()


def test_same_invoice_identity_cannot_be_transmitted_twice(db):
    dest = _destination(db, "adfunture")
    box = f.mailbox(db, "info@adfunture.de")
    doc1 = f.document(db, f.message(db, box, uid=1), b"version A")
    doc2 = f.document(db, f.message(db, box, uid=2), b"version B, andere Datei")
    db.add(_transmission(doc1, dest, identity="a" * 64))
    db.flush()
    db.add(_transmission(doc2, dest, identity="a" * 64))
    with pytest.raises(IntegrityError, match="uq_datev_transmissions_identity_live"):
        db.flush()


def test_test_mode_transmissions_do_not_block_live_send(db):
    dest = _destination(db, "adfunture")
    doc = f.document(db, f.message(db, f.mailbox(db, "info@adfunture.de"), uid=1), b"pdf")
    db.add(_transmission(doc, dest, test_mode=True))
    db.add(_transmission(doc, dest, test_mode=True))
    db.add(_transmission(doc, dest, test_mode=False))
    db.flush()


def test_transmission_cannot_target_other_companys_destination(db):
    doc = f.document(db, f.message(db, f.mailbox(db, "info@adfunture.de"), uid=1), b"pdf")
    sbc_dest = _destination(db, "sbc")
    t = _transmission(doc, sbc_dest)
    db.add(t)
    with pytest.raises(IntegrityError, match="fk_destination_id_same_company"):
        db.flush()


def test_global_setting_unique_despite_null_company(db):
    app_settings.set_setting(db, "datev.test_mode", True)
    app_settings.set_setting(db, "datev.test_mode", False)
    db.flush()
    assert app_settings.get_setting(db, "datev.test_mode") is False
    count = db.scalar(text("SELECT count(*) FROM application_settings WHERE key = 'datev.test_mode'"))
    assert count == 1
