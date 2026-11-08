from __future__ import annotations

import pytest

from .conftest import company_id


def _box(client, email: str) -> dict:
    return next(b for b in client.get("/api/mailboxes").json() if b["email_address"] == email)


def test_list_shows_blockers_without_exposing_secrets(admin, monkeypatch):
    monkeypatch.setenv("INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO", "streng-geheim")
    response = admin.get("/api/mailboxes")
    assert response.status_code == 200
    assert "streng-geheim" not in response.text
    info = _box(admin, "info@adfunture.de")
    assert info["credential_configured"] is True
    assert info["activation_blockers"] == []
    assert [f["folder_name"] for f in info["folders"]] == ["INBOX"]
    sbc = _box(admin, "mail@sbc-symbio.de")
    assert "Domain des Postfachs ist nicht bestätigt" in sbc["activation_blockers"]
    assert "IMAP-Host fehlt" in sbc["activation_blockers"]


def test_company_assignment_cannot_be_changed_via_api(admin):
    box = _box(admin, "info@adfunture.de")
    response = admin.patch(f"/api/mailboxes/{box['id']}", json={"company_id": company_id("sbc")})
    assert response.status_code == 422
    assert _box(admin, "info@adfunture.de")["company_id"] == company_id("adfunture")


def test_activation_requires_secret(admin):
    box = _box(admin, "info@adfunture.de")
    response = admin.post(f"/api/mailboxes/{box['id']}/active", json={"active": True})
    assert response.status_code == 422
    assert "INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO" in response.json()["detail"]


def test_activation_with_secret(admin, monkeypatch):
    monkeypatch.setenv("INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO", "x")
    box = _box(admin, "info@adfunture.de")
    response = admin.post(f"/api/mailboxes/{box['id']}/active", json={"active": True})
    assert response.status_code == 200 and response.json()["is_active"] is True
    response = admin.post(f"/api/mailboxes/{box['id']}/active", json={"active": False})
    assert response.json()["is_active"] is False


def test_sbc_mailbox_needs_domain_confirmation_and_host(admin, monkeypatch):
    monkeypatch.setenv("INVOICEFLOW_SECRET_IMAP_SBC_MAIL", "x")
    box = _box(admin, "mail@sbc-symbio.de")
    url = f"/api/mailboxes/{box['id']}"
    assert admin.post(url + "/active", json={"active": True}).status_code == 422
    assert admin.post(url + "/verify-domain", json={"confirmation": "sbc-symbio.com"}).status_code == 422
    assert admin.post(url + "/verify-domain", json={"confirmation": "sbc-symbio.de"}).status_code == 200
    patch = admin.patch(url, json={"imap_host": "w0123456.kasserver.com", "imap_username": "mail@sbc-symbio.de"})
    assert patch.status_code == 200
    response = admin.post(url + "/active", json={"active": True})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("ref", ["DATABASE_URL", "invoiceflow_secret_x", "INVOICEFLOW_SECRET_", "INVOICEFLOW_SECRET_A-B"])
def test_credential_ref_must_use_secret_prefix(admin, ref):
    box = _box(admin, "info@adfunture.de")
    assert admin.patch(f"/api/mailboxes/{box['id']}", json={"credential_ref": ref}).status_code == 422


def test_create_mailbox_and_duplicate(admin):
    body = {
        "company_id": company_id("adfunture"), "email_address": "Buchhaltung@Adfunture.de", "provider": "strato",
        "imap_host": "imap.strato.de", "imap_username": "buchhaltung@adfunture.de",
        "credential_ref": "INVOICEFLOW_SECRET_IMAP_ADFUNTURE_BUCHHALTUNG",
    }
    created = admin.post("/api/mailboxes", json=body)
    assert created.status_code == 201
    assert created.json()["email_address"] == "buchhaltung@adfunture.de"
    assert created.json()["is_active"] is False
    assert admin.post("/api/mailboxes", json=body).status_code == 409


def test_enable_additional_folder(admin):
    box = _box(admin, "invoice@adfunture.de")
    response = admin.post(f"/api/mailboxes/{box['id']}/folders", json={"folder_name": "Rechnungen", "enabled": True})
    assert response.status_code == 200
    assert {f["folder_name"] for f in response.json()["folders"]} == {"INBOX", "Rechnungen"}


def test_invalid_imap_host_rejected(admin):
    box = _box(admin, "info@adfunture.de")
    assert admin.patch(f"/api/mailboxes/{box['id']}", json={"imap_host": "imap.strato.de/../x"}).status_code == 422


def test_changes_are_audited(admin):
    box = _box(admin, "info@adfunture.de")
    admin.patch(f"/api/mailboxes/{box['id']}", json={"display_name": "Info"})
    logs = admin.get("/api/audit-logs", params={"entity_type": "mailbox"}).json()
    assert logs[0]["action"] == "mailbox.updated"
    assert logs[0]["actor_label"] == "admin@sbc-symbio.de"
