"""Test-Infrastruktur.

Die Tests laufen gegen ein echtes PostgreSQL (TEST_DATABASE_URL). Das Schema
wird über die Alembic-Migrationen aufgebaut, damit Constraints und Trigger
genau so geprüft werden, wie sie in Produktion existieren.

Lokal ohne Docker: ``tools/testdb`` startet PGlite (Postgres 17 in WASM),
siehe README. In CI läuft ein PostgreSQL-Service-Container.
"""
from __future__ import annotations

import base64
import os
import tempfile

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://postgres:postgres@127.0.0.1:54329/postgres?sslmode=disable"
)
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": TEST_DATABASE_URL,
        "COOKIE_SECURE": "false",
        "DATEV_ALLOW_REAL_SEND": "false",
        # PGlite verarbeitet nur eine Verbindung sicher; mit echtem Postgres egal.
        "DB_POOL_SIZE": os.environ.get("TEST_DB_POOL_SIZE", "1"),
        "DB_MAX_OVERFLOW": "0",
        "DB_DISABLE_PREPARED_STATEMENTS": os.environ.get("TEST_DB_DISABLE_PREPARED_STATEMENTS", "true"),
        "RATE_LIMIT_PER_MINUTE": "100000",
        "DOCUMENT_ENCRYPTION_KEY": base64.b64encode(b"k" * 32).decode(),
        "STORAGE_BACKEND": "local",
        "STORAGE_LOCAL_PATH": tempfile.mkdtemp(prefix="invoiceflow-test-storage-"),
        "IMAP_CONNECT_RETRIES": "3",
    }
)

from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from invoiceflow.config import get_settings  # noqa: E402
from invoiceflow.db.enums import UserRole  # noqa: E402
from invoiceflow.db.models import Base, Company, User, UserCompanyAccess  # noqa: E402
from invoiceflow.db.session import get_engine, get_sessionmaker  # noqa: E402
from invoiceflow.security.passwords import hash_password  # noqa: E402
from invoiceflow.seed import seed  # noqa: E402

PASSWORD = "Sicheres-Passwort-2026"


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    from alembic import command
    from alembic.config import Config

    from invoiceflow.cli import _alembic_ini

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(Config(str(_alembic_ini())), "head")
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_database(migrated_database: None) -> Iterator[None]:
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    with get_sessionmaker()() as db:
        seed(db)
        db.commit()
    yield


@pytest.fixture
def db() -> Iterator[Session]:
    """Direkter DB-Zugriff. Vor API-Aufrufen schließen (PGlite: eine Verbindung)."""
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def make_user(email: str, role: UserRole, company_slugs: tuple[str, ...] = ()) -> int:
    with get_sessionmaker()() as s:
        user = User(email=email, display_name=email, password_hash=hash_password(PASSWORD), role=role)
        s.add(user)
        s.flush()
        for slug in company_slugs:
            company_id = s.query(Company.id).filter(Company.slug == slug).scalar()
            s.add(UserCompanyAccess(user_id=user.id, company_id=company_id))
        s.commit()
        return user.id


def company_id(slug: str, db: Session | None = None) -> int:
    """Mit ``db`` innerhalb der offenen Session abfragen (PGlite erlaubt nur eine Verbindung)."""
    if db is not None:
        return db.query(Company.id).filter(Company.slug == slug).scalar()
    with get_sessionmaker()() as s:
        return s.query(Company.id).filter(Company.slug == slug).scalar()


class ApiClient:
    """TestClient mit Login und automatischem CSRF-Header."""

    def __init__(self) -> None:
        from invoiceflow.api.app import create_app

        self.http = TestClient(create_app(get_settings()))
        self.csrf: str | None = None

    def login(self, email: str, password: str = PASSWORD):
        response = self.http.post("/api/auth/login", json={"email": email, "password": password})
        if response.status_code == 200:
            self.csrf = response.json()["csrf_token"]
        return response

    def _headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf} if self.csrf else {}

    def get(self, url: str, **kw):
        return self.http.get(url, **kw)

    def post(self, url: str, json=None, **kw):
        return self.http.post(url, json=json if json is not None else {}, headers=self._headers(), **kw)

    def put(self, url: str, json=None, **kw):
        return self.http.put(url, json=json, headers=self._headers(), **kw)

    def patch(self, url: str, json=None, **kw):
        return self.http.patch(url, json=json, headers=self._headers(), **kw)

    def delete(self, url: str, **kw):
        return self.http.delete(url, headers=self._headers(), **kw)


@pytest.fixture
def admin() -> ApiClient:
    make_user("admin@sbc-symbio.de", UserRole.admin)
    client = ApiClient()
    assert client.login("admin@sbc-symbio.de").status_code == 200
    return client


@pytest.fixture
def anon() -> ApiClient:
    return ApiClient()
