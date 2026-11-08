"""Fehlerüberwachung mit Alarmen.

Läuft als Railway-Cron (alle 10 Minuten). Jede Regel liefert Befunde mit
stabilem Schlüssel; ``alert_states`` merkt sich, was schon gemeldet wurde:

* neuer Befund → sofort melden
* weiterhin aktiv → erneut nach 6 h (kritisch) bzw. 24 h (Warnung)
* verschwunden → Entwarnung

Alarmtexte enthalten nur Anzahlen, IDs und Postfachnamen – keine Rechnungsinhalte.
"""
from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from invoiceflow.config import Settings
from invoiceflow.db.enums import DocumentStatus, JobStatus, TransmissionStatus
from invoiceflow.db.models import (
    AlertState,
    DatevDestination,
    DatevTransmission,
    Document,
    Mailbox,
    ProcessingJob,
    WorkerHeartbeat,
)
from invoiceflow.logging_setup import redact_text
from invoiceflow.security.secrets import MissingSecretError, resolve_secret

log = logging.getLogger("invoiceflow.monitor")

WORKER_TYPES = ("imap", "processing", "datev")
RENOTIFY = {"critical": timedelta(hours=6), "warning": timedelta(hours=24)}


@dataclass(frozen=True)
class Finding:
    key: str
    severity: str  # critical | warning
    message: str


@dataclass
class MonitorResult:
    findings: list[Finding] = field(default_factory=list)
    notified: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    notification_errors: list[str] = field(default_factory=list)


def _count(db: Session, model, *where) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*where)) or 0


def collect_findings(db: Session, settings: Settings, now: datetime | None = None) -> list[Finding]:
    now = now or datetime.now(UTC)
    findings: list[Finding] = []

    # Hintergrunddienste
    for worker_type in WORKER_TYPES:
        last = db.scalar(select(func.max(WorkerHeartbeat.last_beat_at)).where(WorkerHeartbeat.worker_type == worker_type))
        if last is None:
            if settings.is_production:
                findings.append(Finding(f"worker:{worker_type}", "critical", f"Worker „{worker_type}“ hat sich noch nie gemeldet"))
        elif last < now - timedelta(minutes=10):
            minutes = int((now - last).total_seconds() // 60)
            findings.append(Finding(f"worker:{worker_type}", "critical", f"Worker „{worker_type}“ meldet sich seit {minutes} Minuten nicht"))

    # Postfächer
    for mb in db.scalars(select(Mailbox).where(Mailbox.is_active.is_(True))):
        if mb.consecutive_failures >= 3:
            findings.append(Finding(f"mailbox:{mb.id}:failing", "critical",
                                    f"Postfach {mb.email_address}: {mb.consecutive_failures} Abrufe in Folge fehlgeschlagen"))
        elif mb.last_successful_sync_at and mb.last_successful_sync_at < now - timedelta(hours=1):
            findings.append(Finding(f"mailbox:{mb.id}:stale", "warning",
                                    f"Postfach {mb.email_address}: seit über 1 Stunde kein erfolgreicher Abruf"))

    # Verarbeitung
    dead = _count(db, ProcessingJob, ProcessingJob.status == JobStatus.dead)
    if dead:
        findings.append(Finding("jobs:dead", "critical", f"{dead} Verarbeitungsjob(s) endgültig fehlgeschlagen"))

    # DATEV
    live = DatevTransmission.test_mode.is_(False)
    unclear = _count(db, DatevTransmission, live, DatevTransmission.status == TransmissionStatus.manual_review)
    if unclear:
        findings.append(Finding("datev:unclear", "critical", f"{unclear} DATEV-Übertragung(en) mit unklarem Ergebnis – bitte klären"))
    failed = _count(db, DatevTransmission, live, DatevTransmission.status == TransmissionStatus.failed)
    if failed:
        findings.append(Finding("datev:failed", "warning", f"{failed} DATEV-Übertragung(en) fehlgeschlagen"))
    old_unconfirmed = _count(db, DatevTransmission, live, DatevTransmission.status == TransmissionStatus.sent_unconfirmed,
                             DatevTransmission.sent_at < now - timedelta(days=3))
    if old_unconfirmed:
        findings.append(Finding("datev:unconfirmed", "warning",
                                f"{old_unconfirmed} DATEV-Übertragung(en) seit über 3 Tagen unbestätigt"))
    auto_companies = list(db.scalars(select(DatevDestination.company_id).where(DatevDestination.auto_send_enabled.is_(True))))
    if auto_companies:
        stuck = _count(db, DatevTransmission, live, DatevTransmission.status == TransmissionStatus.pending,
                       DatevTransmission.company_id.in_(auto_companies), DatevTransmission.created_at < now - timedelta(days=1))
        if stuck:
            findings.append(Finding("datev:stuck", "warning",
                                    f"{stuck} Beleg(e) warten trotz Auto-Versand seit über 1 Tag (Grund in der Übertragung)"))

    # Rückstände
    review = _count(db, Document, Document.status == DocumentStatus.manual_review, Document.updated_at < now - timedelta(days=2))
    if review:
        findings.append(Finding("review:backlog", "warning", f"{review} Dokument(e) seit über 2 Tagen in manueller Prüfung"))
    xml = _count(db, Document, Document.status == DocumentStatus.awaiting_datev_xml_integration,
                 Document.created_at < now - timedelta(days=7))
    if xml:
        findings.append(Finding("xml:backlog", "warning", f"{xml} XML-Rechnung(en) seit über 7 Tagen in der Warteschlange"))
    return findings


# --- Benachrichtigung ----------------------------------------------------------


class Notifier(Protocol):
    name: str

    def send(self, subject: str, body: str) -> None: ...


class EmailNotifier:
    name = "email"

    def __init__(self, settings: Settings) -> None:
        self.s = settings

    def send(self, subject: str, body: str) -> None:
        s = self.s
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = s.alert_email_from, ", ".join(s.alert_email_to), subject
        msg.set_content(body)
        context = ssl.create_default_context()
        password = resolve_secret(s.alert_smtp_credential_ref) if s.alert_smtp_credential_ref else None
        if s.alert_smtp_tls_mode == "ssl":
            smtp = smtplib.SMTP_SSL(s.alert_smtp_host, s.alert_smtp_port, context=context, timeout=30)
        else:
            smtp = smtplib.SMTP(s.alert_smtp_host, s.alert_smtp_port, timeout=30)
            smtp.starttls(context=context)
        with smtp:
            if s.alert_smtp_username and password:
                smtp.login(s.alert_smtp_username, password)
            smtp.send_message(msg)


class TelegramNotifier:
    name = "telegram"
    API = "https://api.telegram.org"  # feste Gegenstelle, keine URL aus Daten

    def __init__(self, settings: Settings) -> None:
        self.chat_id = settings.alert_telegram_chat_id
        self.token_ref = settings.alert_telegram_token_ref

    def send(self, subject: str, body: str) -> None:
        token = resolve_secret(self.token_ref)
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": f"{subject}\n\n{body}"[:4000]}).encode()
        request = urllib.request.Request(f"{self.API}/bot{token}/sendMessage", data=data, method="POST")
        with urllib.request.urlopen(request, timeout=20, context=ssl.create_default_context()) as response:  # noqa: S310
            payload = json.loads(response.read().decode())
            if not payload.get("ok"):
                raise RuntimeError("Telegram hat die Nachricht abgelehnt")


def configured_notifiers(settings: Settings) -> list[Notifier]:
    notifiers: list[Notifier] = []
    if settings.alert_email_to and settings.alert_email_from and settings.alert_smtp_host:
        notifiers.append(EmailNotifier(settings))
    if settings.alert_telegram_chat_id and settings.alert_telegram_token_ref:
        notifiers.append(TelegramNotifier(settings))
    return notifiers


def run_monitor(db: Session, settings: Settings, notifiers: list[Notifier], now: datetime | None = None) -> MonitorResult:
    now = now or datetime.now(UTC)
    result = MonitorResult(findings=collect_findings(db, settings, now))
    states = {s.key: s for s in db.scalars(select(AlertState))}
    to_notify: list[Finding] = []

    for f in result.findings:
        state = states.get(f.key)
        if state is None:
            state = AlertState(key=f.key, severity=f.severity, message=f.message, active=True, first_seen_at=now, last_seen_at=now)
            db.add(state)
            states[f.key] = state
            to_notify.append(f)
        else:
            was_active = state.active
            state.severity, state.message, state.active, state.last_seen_at, state.resolved_at = f.severity, f.message, True, now, None
            if not was_active:
                state.first_seen_at = now
            due = state.last_notified_at is None or state.last_notified_at < now - RENOTIFY.get(f.severity, RENOTIFY["warning"])
            if not was_active or due:
                to_notify.append(f)

    active_keys = {f.key for f in result.findings}
    resolved = [s for s in states.values() if s.active and s.key not in active_keys and s.last_seen_at < now]
    for s in resolved:
        s.active = False
        s.resolved_at = now
    result.resolved = [s.key for s in resolved]

    if (to_notify or resolved) and notifiers:
        lines = [f"{'🔴' if f.severity == 'critical' else '🟡'} {f.message}" for f in to_notify]
        lines += [f"✅ Behoben: {s.message}" for s in resolved]
        if settings.public_base_url:
            lines.append(f"\nDashboard: {settings.public_base_url.rstrip('/')}/")
        critical = any(f.severity == "critical" for f in to_notify)
        subject = f"InvoiceFlow: {'KRITISCH' if critical else 'Hinweis' if to_notify else 'Entwarnung'} ({len(to_notify)} offen)"
        delivered = False
        for notifier in notifiers:
            try:
                notifier.send(subject, "\n".join(lines))
                delivered = True
            except (OSError, smtplib.SMTPException, MissingSecretError, RuntimeError) as exc:
                result.notification_errors.append(f"{notifier.name}: {redact_text(str(exc))[:300]}")
                log.error("alert_delivery_failed", extra={"channel": notifier.name, "error": redact_text(str(exc))})
        if delivered:
            for f in to_notify:
                states[f.key].last_notified_at = now
            result.notified = [f.key for f in to_notify]
    elif to_notify:
        log.warning("alerts_without_channel", extra={"alerts": [f.message for f in to_notify]})
    for f in result.findings:
        log.log(logging.ERROR if f.severity == "critical" else logging.WARNING, "monitor_finding",
                extra={"key": f.key, "severity": f.severity, "detail": f.message})
    return result
