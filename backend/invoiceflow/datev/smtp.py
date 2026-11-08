"""SMTP-Versand an DATEV Upload Mail mit eindeutiger Ergebnis-Klassifikation.

Entscheidend ist der Zeitpunkt eines Fehlers:

* **vor DATA** (Verbindung, TLS, Login, MAIL FROM, RCPT TO): sicher nichts
  zugestellt → je nach Antwort wiederholbar (4xx/Netz) oder endgültig (5xx).
* **während/nach DATA ohne Abschlussantwort**: Der Server hat die Mail
  möglicherweise angenommen → ``unknown``. Kein automatischer Zweitversand.
* Abschlussantwort 250 → ``accepted``. Das bedeutet nur, dass der
  Mailserver die Mail übernommen hat – **nicht**, dass DATEV sie verarbeitet hat.
"""
from __future__ import annotations

import os
import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from invoiceflow.db.enums import AttemptOutcome, TlsMode


@dataclass(frozen=True)
class SmtpParams:
    host: str
    port: int
    tls_mode: TlsMode
    username: str
    password: str
    timeout: float = 60.0


@dataclass(frozen=True)
class SmtpResult:
    outcome: AttemptOutcome
    code: int | None = None
    response: str | None = None
    error: str | None = None


class Transport(Protocol):
    def send(self, message: EmailMessage, params: SmtpParams, envelope_from: str, recipient: str) -> SmtpResult: ...


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")[:1000]
    return str(value)[:1000]


def _classify(code: int, response) -> SmtpResult:
    if 200 <= code < 300:
        return SmtpResult(AttemptOutcome.accepted, code, _text(response))
    if 400 <= code < 500:
        return SmtpResult(AttemptOutcome.temporary_failure, code, _text(response))
    return SmtpResult(AttemptOutcome.rejected, code, _text(response))


class SmtpTransport:
    """Echter Versand über smtplib (TLS mit Zertifikatsprüfung)."""

    def __init__(self) -> None:
        # Zweite Sicherung neben der Konfigurationsprüfung: Tests senden nie echte Mails.
        if os.environ.get("APP_ENV") == "test":
            raise RuntimeError("SmtpTransport ist in der Testumgebung gesperrt")

    def send(self, message: EmailMessage, params: SmtpParams, envelope_from: str, recipient: str) -> SmtpResult:
        context = ssl.create_default_context()
        try:
            if params.tls_mode == TlsMode.ssl:
                smtp = smtplib.SMTP_SSL(params.host, params.port, context=context, timeout=params.timeout)
            else:
                smtp = smtplib.SMTP(params.host, params.port, timeout=params.timeout)
                smtp.ehlo()
                smtp.starttls(context=context)
            smtp.ehlo()
        except (OSError, smtplib.SMTPException) as exc:
            return SmtpResult(AttemptOutcome.temporary_failure, error=f"Verbindung: {type(exc).__name__}: {exc}"[:500])

        try:
            try:
                smtp.login(params.username, params.password)
            except smtplib.SMTPAuthenticationError as exc:
                return SmtpResult(AttemptOutcome.rejected, exc.smtp_code, _text(exc.smtp_error), "SMTP-Anmeldung abgelehnt")
            code, resp = smtp.mail(envelope_from)
            if code != 250:
                smtp.rset()
                return _classify(code, resp)
            code, resp = smtp.rcpt(recipient)
            if code not in (250, 251):
                smtp.rset()
                return _classify(code, resp)
            # --- ab hier keine sichere Aussage mehr bei Abbruch -------------------
            try:
                code, resp = smtp.data(message.as_bytes())
            except smtplib.SMTPDataError as exc:
                # DATA wurde nicht mit 354 beantwortet → Inhalt wurde nicht übertragen.
                return _classify(exc.smtp_code, exc.smtp_error)
            except (OSError, socket.timeout, smtplib.SMTPServerDisconnected) as exc:
                return SmtpResult(AttemptOutcome.unknown, error=f"Abbruch während DATA: {type(exc).__name__}: {exc}"[:500])
            return _classify(code, resp)
        except (OSError, smtplib.SMTPServerDisconnected) as exc:
            return SmtpResult(AttemptOutcome.temporary_failure, error=f"{type(exc).__name__}: {exc}"[:500])
        except smtplib.SMTPResponseException as exc:
            return _classify(exc.smtp_code, exc.smtp_error)
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001 – Abmelden ist best effort
                smtp.close()
