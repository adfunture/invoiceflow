from __future__ import annotations


def test_liveness(anon):
    assert anon.get("/health").json() == {"status": "ok", "checks": {"process": "ok"}}


def test_readiness_checks_db_and_schema(anon):
    response = anon.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok", "schema": "ok"}


def test_security_headers(anon):
    response = anon.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
