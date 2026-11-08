from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from invoiceflow.db.enums import UserRole
from invoiceflow.db.models import UserSession
from invoiceflow.db.session import get_sessionmaker

from .conftest import ApiClient, company_id, make_user


def test_login_and_me(admin):
    me = admin.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["role"] == "admin"
    cookie = admin.http.cookies.get("invoiceflow_session")
    assert cookie and len(cookie) > 30


def test_session_cookie_flags(anon):
    make_user("viewer@adfunture.de", UserRole.viewer, ("adfunture",))
    response = anon.login("viewer@adfunture.de")
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header and "samesite=strict" in header


def test_wrong_password_is_rejected(anon):
    make_user("viewer@adfunture.de", UserRole.viewer)
    assert anon.login("viewer@adfunture.de", "falsch").status_code == 401
    assert anon.login("niemand@adfunture.de", "falsch").status_code == 401
    assert anon.get("/api/auth/me").status_code == 401


def test_account_locked_after_repeated_failures(anon):
    make_user("viewer@adfunture.de", UserRole.viewer)
    for _ in range(5):
        assert anon.login("viewer@adfunture.de", "falsch").status_code == 401
    # Auch das richtige Passwort hilft während der Sperre nicht.
    assert anon.login("viewer@adfunture.de").status_code == 429


def test_unauthenticated_requests_rejected(anon):
    for url in ("/api/companies", "/api/mailboxes", "/api/datev/destinations", "/api/settings"):
        assert anon.get(url).status_code == 401


def test_csrf_header_required_for_mutations(admin):
    box_id = admin.get("/api/mailboxes").json()[0]["id"]
    response = admin.http.patch(f"/api/mailboxes/{box_id}", json={"display_name": "x"})  # ohne Header
    assert response.status_code == 403
    response = admin.http.patch(f"/api/mailboxes/{box_id}", json={"display_name": "x"}, headers={"X-CSRF-Token": "falsch"})
    assert response.status_code == 403
    assert admin.patch(f"/api/mailboxes/{box_id}", json={"display_name": "x"}).status_code == 200


def test_logout_invalidates_session(admin):
    assert admin.post("/api/auth/logout").status_code == 204
    assert admin.get("/api/auth/me").status_code == 401


def test_idle_session_expires(admin):
    with get_sessionmaker()() as s:
        s.execute(update(UserSession).values(last_seen_at=datetime.now(UTC) - timedelta(hours=3)))
        s.commit()
    assert admin.get("/api/auth/me").status_code == 401


def test_viewer_cannot_change_configuration():
    make_user("viewer@adfunture.de", UserRole.viewer, ("adfunture",))
    client = ApiClient()
    client.login("viewer@adfunture.de")
    box_id = client.get("/api/mailboxes").json()[0]["id"]
    assert client.patch(f"/api/mailboxes/{box_id}", json={"display_name": "x"}).status_code == 403
    assert client.put("/api/settings/datev.test_mode", json={"value": False}).status_code == 403


def test_non_admin_sees_only_assigned_company():
    make_user("buchhaltung@adfunture.de", UserRole.reviewer, ("adfunture",))
    client = ApiClient()
    client.login("buchhaltung@adfunture.de")
    companies = client.get("/api/companies").json()
    assert [c["slug"] for c in companies] == ["adfunture"]
    boxes = client.get("/api/mailboxes").json()
    assert {b["email_address"] for b in boxes} == {"info@adfunture.de", "invoice@adfunture.de"}
    assert all(d["company_id"] == company_id("adfunture") for d in client.get("/api/datev/destinations").json())
    # Fremdes Unternehmen: 404 statt 403
    assert client.get(f"/api/companies/{company_id('sbc')}").status_code == 404
    assert client.get("/api/mailboxes", params={"company_id": company_id("sbc")}).status_code == 404


def test_change_password_revokes_other_sessions(admin):
    other = ApiClient()
    other.login("admin@sbc-symbio.de")
    response = admin.post("/api/auth/change-password",
                          json={"current_password": "Sicheres-Passwort-2026", "new_password": "Noch-Sichereres-2027!"})
    assert response.status_code == 204
    assert admin.get("/api/auth/me").status_code == 200
    assert other.get("/api/auth/me").status_code == 401


def test_weak_password_rejected(admin):
    response = admin.post("/api/auth/change-password",
                          json={"current_password": "Sicheres-Passwort-2026", "new_password": "aaaaaaaaaaaa"})
    assert response.status_code == 422


def test_garbage_forwarded_for_does_not_crash(anon):
    make_user("viewer@adfunture.de", UserRole.viewer)
    response = anon.http.post("/api/auth/login", json={"email": "viewer@adfunture.de", "password": "falsch"},
                              headers={"X-Forwarded-For": "<script>, nicht-eine-ip"})
    assert response.status_code == 401


def test_login_with_internal_domain(anon):
    make_user("admin@firma.test", UserRole.admin)
    assert anon.login("admin@firma.test").status_code == 200
    assert anon.login("kein-at-zeichen", "x").status_code == 422
