from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from invoiceflow.config import AppEnv, Settings
from invoiceflow.db.models import Company, DatevDestination, Mailbox, MailboxSyncState
from invoiceflow.logging_setup import redact_text, redact_value
from invoiceflow.seed import seed
from invoiceflow.services.datev_gate import address_format_warnings


def test_seed_creates_both_companies_and_mailboxes(db):
    companies = {c.slug: c for c in db.scalars(select(Company))}
    assert set(companies) == {"sbc", "adfunture"}
    boxes = {m.email_address: m for m in db.scalars(select(Mailbox))}
    assert set(boxes) == {
        "mail@sbc-symbio.de", "sascha.senninger@sbc-symbio.de", "invoice@sbc-symbio.de",
        "info@adfunture.de", "invoice@adfunture.de",
    }
    assert boxes["info@adfunture.de"].company_id == companies["adfunture"].id
    assert boxes["invoice@sbc-symbio.de"].company_id == companies["sbc"].id
    assert boxes["info@adfunture.de"].imap_host == "imap.strato.de"
    assert all(not m.is_active for m in boxes.values()), "Postfächer starten deaktiviert"
    assert boxes["mail@sbc-symbio.de"].domain_verified_at is None
    assert boxes["sascha.senninger@sbc-symbio.de"].domain_verified_at is None
    # Jedes Postfach hat INBOX als ersten Ordner.
    assert db.scalar(select(func.count()).select_from(MailboxSyncState).where(MailboxSyncState.folder_name == "INBOX")) == 5


def test_seed_is_idempotent(db):
    assert seed(db) == {"companies": 0, "mailboxes": 0, "datev_destinations": 0}
    assert db.scalar(select(func.count()).select_from(Mailbox)) == 5


def test_datev_destinations_start_unverified_and_disabled(db):
    for dest in db.scalars(select(DatevDestination)):
        assert dest.address_verified_at is None
        assert dest.production_released_at is None
        assert dest.auto_send_enabled is False
        assert dest.target_address.endswith("@uploadmail.datev.de")


def test_screenshot_addresses_are_flagged():
    """Beide Adressen aus dem Screenshot haben kein UUID-Format."""
    for address in (
        "5462c653-86b9-4c24-b95a-8962c-f4235f1@uploadmail.datev.de",
        "90f44e89-d150-453a-a050-ea0-fe82180f6@uploadmail.datev.de",
    ):
        assert any("UUID" in w for w in address_format_warnings(address))
    assert address_format_warnings("5462c653-86b9-4c24-b95a-8962cf4235f1@uploadmail.datev.de") == []


def test_import_start_is_midnight_berlin():
    start = Settings(app_env=AppEnv.test).import_start_at
    assert start.isoformat() == "2026-11-08T00:00:00+01:00"
    assert start.astimezone(UTC) == datetime(2026, 11, 7, 23, 0, tzinfo=UTC)


def test_railway_database_url_is_normalized():
    s = Settings(app_env=AppEnv.test, database_url="postgresql://u:p@host:5432/db")
    assert s.database_url == "postgresql+psycopg://u:p@host:5432/db"


def test_real_send_forbidden_in_test_env():
    with pytest.raises(ValueError):
        Settings(app_env=AppEnv.test, datev_allow_real_send=True)


def test_production_requires_secure_cookie():
    with pytest.raises(ValueError):
        Settings(app_env=AppEnv.production, cookie_secure=False)


def test_logs_redact_credentials():
    assert redact_text("login password=hunter2 ok") == "login password=*** ok"
    assert redact_text("postgresql://user:geheim@db:5432/x") == "postgresql://user:***@db:5432/x"
    assert redact_value("details", {"smtp_password": "x", "host": "smtp.strato.de"}) == {
        "smtp_password": "***", "host": "smtp.strato.de"
    }


@pytest.mark.parametrize("raw,expected", [
    ('["a.example.de", "b.example.de"]', ["a.example.de", "b.example.de"]),
    ("a.example.de, b.example.de", ["a.example.de", "b.example.de"]),
    ("[web-production-21eb5.up.railway.app]", ["web-production-21eb5.up.railway.app"]),
    ("a.example.de", ["a.example.de"]),
])
def test_list_settings_accept_json_and_comma_separated(monkeypatch, raw, expected):
    monkeypatch.setenv("ALLOWED_HOSTS", raw)
    monkeypatch.setenv("ALERT_EMAIL_TO", raw)
    s = Settings(app_env=AppEnv.test)
    assert s.allowed_hosts == expected and s.alert_email_to == expected
