"""Produktivfreigabe DATEV: automatischer Versand bleibt aus, bis alle Schritte dokumentiert sind."""
from __future__ import annotations

from .conftest import company_id

STEPS = ("sender_authorized", "assignment_tested", "duplicate_check_tested", "test_delivery_confirmed")
CORRECTED = "90f44e89-d150-453a-a050-ea0fe82180f6@uploadmail.datev.de"


def _dest(client, slug: str) -> dict:
    return next(d for d in client.get("/api/datev/destinations").json() if d["company_id"] == company_id(slug))


def _sender(client, dest_id: int):
    return client.put(f"/api/datev/destinations/{dest_id}/sender", json={
        "sender_address": "invoice@adfunture.de", "smtp_host": "smtp.strato.de", "smtp_port": 465,
        "smtp_tls_mode": "ssl", "smtp_username": "invoice@adfunture.de",
        "smtp_credential_ref": "INVOICEFLOW_SECRET_SMTP_ADFUNTURE_INVOICE",
    })


def _full_release(client, dest_id: int) -> dict:
    url = f"/api/datev/destinations/{dest_id}"
    assert client.put(url + "/address", json={"target_address": CORRECTED}).status_code == 200
    assert client.post(url + "/verify-address", json={"confirmation": CORRECTED}).status_code == 200
    assert _sender(client, dest_id).status_code == 200
    for step in STEPS:
        assert client.post(f"{url}/gate/{step}", json={}).status_code == 200, step
    response = client.post(url + "/release", json={"acknowledgement": "PRODUKTIV FREIGEBEN"})
    assert response.status_code == 200, response.text
    return response.json()


def test_initial_state_blocks_sending(admin):
    dest = _dest(admin, "adfunture")
    assert dest["send_allowed"] is False
    assert dest["test_mode"] is True
    assert "Testmodus ist aktiv" in dest["send_blockers"]
    assert "DATEV_ALLOW_REAL_SEND ist nicht true" in dest["send_blockers"]
    assert any("UUID" in w for w in dest["address_format_warnings"])


def test_verification_requires_exact_retype(admin):
    dest = _dest(admin, "adfunture")
    url = f"/api/datev/destinations/{dest['id']}/verify-address"
    assert admin.post(url, json={"confirmation": CORRECTED}).status_code == 422
    ok = admin.post(url, json={"confirmation": dest["target_address"].upper()})
    assert ok.status_code == 200
    assert ok.json()["gate_steps"][0]["done_at"] is not None


def test_steps_require_verified_address(admin):
    dest = _dest(admin, "adfunture")
    response = admin.post(f"/api/datev/destinations/{dest['id']}/gate/assignment_tested", json={})
    assert response.status_code == 422


def test_sender_step_requires_sender_config(admin):
    dest = _dest(admin, "sbc")
    url = f"/api/datev/destinations/{dest['id']}"
    admin.post(url + "/verify-address", json={"confirmation": dest["target_address"]})
    assert admin.post(url + "/gate/sender_authorized", json={}).status_code == 422


def test_release_requires_all_steps_and_acknowledgement(admin):
    dest = _dest(admin, "adfunture")
    url = f"/api/datev/destinations/{dest['id']}"
    assert admin.post(url + "/release", json={"acknowledgement": "PRODUKTIV FREIGEBEN"}).status_code == 422
    assert admin.post(url + "/auto-send", json={"enabled": True}).status_code == 422


def test_full_release_and_auto_send_still_blocked_by_test_mode(admin):
    dest = _dest(admin, "adfunture")
    released = _full_release(admin, dest["id"])
    assert released["production_released_at"] is not None
    response = admin.post(f"/api/datev/destinations/{dest['id']}/auto-send", json={"enabled": True})
    assert response.status_code == 200
    body = response.json()
    assert body["auto_send_enabled"] is True
    # Testmodus und fehlende Env-Freigabe blockieren weiterhin.
    assert body["send_allowed"] is False
    assert "Testmodus ist aktiv" in body["send_blockers"]
    assert "DATEV_ALLOW_REAL_SEND ist nicht true" in body["send_blockers"]


def test_wrong_acknowledgement_rejected(admin):
    dest = _dest(admin, "adfunture")
    url = f"/api/datev/destinations/{dest['id']}"
    admin.put(url + "/address", json={"target_address": CORRECTED})
    admin.post(url + "/verify-address", json={"confirmation": CORRECTED})
    _sender(admin, dest["id"])
    for step in STEPS:
        admin.post(f"{url}/gate/{step}", json={})
    assert admin.post(url + "/release", json={"acknowledgement": "ja"}).status_code == 422


def test_address_change_resets_everything(admin):
    dest = _dest(admin, "adfunture")
    _full_release(admin, dest["id"])
    admin.post(f"/api/datev/destinations/{dest['id']}/auto-send", json={"enabled": True})
    other = "11111111-2222-3333-4444-555555555555@uploadmail.datev.de"
    body = admin.put(f"/api/datev/destinations/{dest['id']}/address", json={"target_address": other}).json()
    assert body["auto_send_enabled"] is False
    assert body["production_released_at"] is None
    assert all(step["done_at"] is None for step in body["gate_steps"])


def test_sender_change_resets_from_sender_step(admin):
    dest = _dest(admin, "adfunture")
    _full_release(admin, dest["id"])
    body = _sender(admin, dest["id"]).json()
    steps = {s["key"]: s["done_at"] for s in body["gate_steps"]}
    assert steps["address_verified"] is not None
    assert all(steps[s] is None for s in STEPS)
    assert body["production_released_at"] is None


def test_non_datev_target_rejected(admin):
    dest = _dest(admin, "adfunture")
    response = admin.put(f"/api/datev/destinations/{dest['id']}/address", json={"target_address": "buchhaltung@angreifer.example"})
    assert response.status_code == 422


def test_test_mode_setting_toggle_is_audited(admin):
    assert admin.put("/api/settings/datev.test_mode", json={"value": "nein"}).status_code == 422
    assert admin.put("/api/settings/datev.test_mode", json={"value": False}).status_code == 200
    log = admin.get("/api/audit-logs", params={"entity_type": "setting"}).json()[0]
    assert log["details"] == {"old": True, "new": False}
