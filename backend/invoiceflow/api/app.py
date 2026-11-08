"""FastAPI-Anwendung: API, Sicherheits-Middleware und Auslieferung des Dashboards."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from invoiceflow.api.deps import client_ip
from invoiceflow.api.routers import (
    admin,
    auth,
    companies,
    dashboard,
    datev,
    documents,
    health,
    mailboxes,
    messages,
    transmissions,
)
from invoiceflow.config import Settings, get_settings
from invoiceflow.logging_setup import configure_logging

log = logging.getLogger("invoiceflow.http")

# React-Build (frontend/dist), im Docker-Image nach /app/frontend/dist kopiert.
FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"

RAILWAY_HEALTHCHECK_HOSTS = ["healthcheck.railway.app"]

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
        "frame-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
}


class RateLimiter:
    """Token Bucket je Client-IP. Gilt pro Web-Instanz (Railway: eine Instanz)."""

    def __init__(self, per_minute: int) -> None:
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens < 1:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1, now)
            if len(self._buckets) > 10_000:  # Speicher begrenzen
                self._buckets.clear()
            return True


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(
        title="InvoiceFlow",
        version="0.1.0",
        docs_url=None if settings.is_production else "/api/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/api/openapi.json",
    )
    limiter = RateLimiter(settings.rate_limit_per_minute)

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        started = time.perf_counter()
        is_health = request.url.path.startswith("/health")

        # Railway terminiert TLS und setzt X-Forwarded-Proto. Klartext-Zugriffe in Produktion abweisen.
        if settings.is_production and not is_health and request.headers.get("x-forwarded-proto", "https") != "https":
            return JSONResponse({"detail": "HTTPS erforderlich"}, status_code=400)
        if not is_health and not limiter.allow(client_ip(request) or "unknown"):
            return JSONResponse({"detail": "Zu viele Anfragen"}, status_code=429, headers={"Retry-After": "60"})

        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = request_id
        if not is_health:
            log.info(
                "request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,  # ohne Query-String
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
        return response

    if settings.is_production and settings.allowed_hosts == ["*"]:
        raise RuntimeError('ALLOWED_HOSTS muss in Produktion gesetzt sein, z. B. ["invoiceflow.example.de"]')
    if settings.allowed_hosts != ["*"]:
        # Railway ruft den Healthcheck mit diesem Host-Header auf.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=[*settings.allowed_hosts, *RAILWAY_HEALTHCHECK_HOSTS])

    for router in (health.router, auth.router, companies.router, mailboxes.router, datev.router, admin.router,
                   messages.router, documents.router, transmissions.router, dashboard.router):
        app.include_router(router)

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    if not (FRONTEND_DIST / "index.html").exists():
        return
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path.startswith(("api/", "health")):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = (FRONTEND_DIST / path).resolve()
        if path and candidate.is_file() and FRONTEND_DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")


app = create_app()
