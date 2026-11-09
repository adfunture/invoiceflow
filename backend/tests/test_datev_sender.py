"""DATEV-Versand. Es wird nie echt gesendet: Transport ist ein Fake, SmtpTransport ist in Tests gesperrt.

Nummern in Klammern verweisen auf die Testfälle aus dem Masterprompt (Abschnitt 14).
"""
from __future__ import annotations

import os
import smtplib
import threading
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest
from sqlalchemy import func, select, update

from invoiceflow.config import AppEnv, Settings
from invoiceflow.datev import operations as ops
from invoiceflow.datev.sender import DatevSender
from invoiceflow.datev.smtp import SmtpParams, SmtpResult, SmtpTransport
from invoiceflow.db.enums import AttemptOutcome, DocumentStatus, TlsMode, TransmissionStatus
from invoiceflow.db.models import DatevDestination, DatevTransmission, DatevTransmissionAttempt, Document
from invoiceflow.db.session import get_sessionmaker
from invoiceflow.services import app_settings
from invoiceflow.services.audit import Actor
from invoiceflow.storage import get_store

from .conftest import company_id
from .samples import cii_xml, invoice_lines, make_pdf, with_embedded_xml
from .test_imap_sync import INFO, INVOICE, accounts  # noqa: F401 – Fixture
from .test_pipeline import PDF, deliver, documents, process

TARGET = "90f44e89-d150-453a-a050-ea0fe82180f6@uploadmail.datev.de"
SECRET = "INVOICEFLOW_SECRET_SMTP_ADFUNTURE_INVOICE"
ACTOR = Actor.system("test")


class FakeTransport:
    """Nimmt Mails entgegen statt sie zu senden; Ergebnisse sind steuerbar."""

    def __init__(self, *outcomes: SmtpResult) -> None:
        self.outcomes = list(outcomes)
        self.sent: list[tuple[EmailMessage, SmtpParams, str, str]] = []
        self.lock = threading.Lock()

    def __call__(self):  # als transport_factory verwendbar
        return self

    def send(self, message, params, envelope_from, recipient) -> SmtpResult:
        with self.lock:
            self.sent.append((message, params, envelope_from, recipient))
            return self.outcomes.pop(0) if self.outcomes else SmtpResult(AttemptOutcome.accepted, 250, "2.0.0 Ok: queued")


def real_send_settings() -> Settings:
    # Nur die Versandsperre auf Infrastrukturebene ist hier "offen"; gesendet wird über den Fake.
    return Settings(app_env=AppEnv.development, datev_allow_real_send=True, cookie_secure=False)


def sender(transport: FakeTransport, settings: Settings | None = None, worker="datev-test") -> DatevSender:
    return DatevSender(get_sessionmaker(), get_store(), settings or real_send_settings(), transport, worker)


def configure_destination(monkeypatch, *, steps=("address_verified", "sender_authorized", "assignment_tested",
                                                   "duplicate_check_tested", "test_delivery_confirmed"),
                          release=True, auto=True, test_mode=False) -> None:
    monkeypatch.setenv(SECRET, "smtp-geheim")
    now = datetime.now(UTC)
    with get_sessionmaker()() as s:
        dest = s.scalar(select(DatevDestination).where(DatevDestination.company_id == company_id("adfunture", s)))
        dest.target_address = TARGET
        dest.sender_address = "invoice@adfunture.de"
        dest.smtp_host, dest.smtp_port, dest.smtp_tls_mode = "smtp.strato.de", 465, TlsMode.ssl
        dest.smtp_username, dest.smtp_credential_ref = "invoice@adfunture.de", SECRET
        for step in steps:
            setattr(dest, f"{step}_at", now)
        if release:
            dest.production_released_at = now
            dest.auto_send_enabled = auto
        app_settings.set_setting(s, "datev.test_mode", test_mode)
        s.commit()


def transmissions() -> list[DatevTransmission]:
    with get_sessionmaker()() as s:
        return list(s.scalars(select(DatevTransmission).where(DatevTransmission.test_mode.is_(False)).order_by(DatevTransmission.id)))


def attempts(tid: int) -> list[DatevTransmissionAttempt]:
    with get_sessionmaker()() as s:
        return list(s.scalars(select(DatevTransmissionAttempt).where(DatevTransmissionAttempt.transmission_id == tid)
                              .order_by(DatevTransmissionAttempt.attempt_no)))


def ready_invoice(accounts, number="RE-2026-1001", mailbox=INFO):
    deliver(accounts, [(f"{number}.pdf", make_pdf(invoice_lines(number)), PDF)], mailbox=mailbox)
    process(accounts, mailbox)


def make_due(tid: int) -> None:
    with get_sessionmaker()() as s:
        s.execute(update(DatevTransmission).where(DatevTransmission.id == tid)
                  .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1)))
        s.commit()


# --- Sperren ---------------------------------------------------------------------


def test_default_configuration_never_sends(accounts):
    ready_invoice(accounts)
    transport = FakeTransport()
    result = sender(transport, Settings(app_env=AppEnv.test, cookie_secure=False)).run_cycle()
    assert result.planned == 1 and result.sent == 0 and transport.sent == []
    t, = transmissions()
    assert t.status == TransmissionStatus.pending
    assert "Testmodus ist aktiv" in t.last_error and "DATEV_ALLOW_REAL_SEND" in t.last_error


def test_test_mode_blocks_even_when_everything_released(accounts, monkeypatch):
    configure_destination(monkeypatch, test_mode=True)
    ready_invoice(accounts)
    transport = FakeTransport()
    sender(transport).run_cycle()
    assert transport.sent == [] and transmissions()[0].status == TransmissionStatus.pending


def test_env_switch_blocks_even_when_everything_released(accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    transport = FakeTransport()
    sender(transport, Settings(app_env=AppEnv.development, cookie_secure=False)).run_cycle()
    assert transport.sent == []


def test_without_auto_send_nothing_is_sent_automatically(accounts, monkeypatch):
    configure_destination(monkeypatch, auto=False)
    ready_invoice(accounts)
    transport = FakeTransport()
    sender(transport).run_cycle()
    assert transport.sent == [] and "Automatischer Versand" in transmissions()[0].last_error


def test_smtp_transport_refuses_in_test_environment():
    with pytest.raises(RuntimeError):
        SmtpTransport()


# --- Erfolgreicher Versand ---------------------------------------------------------


def test_released_invoice_is_sent_once_to_configured_address(accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    doc, _ = documents()[0]
    transport = FakeTransport()
    assert sender(transport).run_cycle().sent == 1
    message, params, envelope_from, recipient = transport.sent[0]
    assert recipient == TARGET and message["To"] == TARGET
    assert envelope_from == "invoice@adfunture.de" and params.password == "smtp-geheim"
    attachment = message.get_payload()[1]
    assert attachment.get_content_type() == PDF and attachment.get_filename() == "RE-2026-1001.pdf"
    assert attachment.get_payload(decode=True) == get_store().get(doc.storage_key), "Original unverändert"
    t, = transmissions()
    assert t.status == TransmissionStatus.sent_unconfirmed and t.smtp_message_id and t.destination_address == TARGET
    assert [a.outcome for a in attempts(t.id)] == [AttemptOutcome.accepted]
    assert documents()[0][0].status == DocumentStatus.transmitted
    # Weitere Durchläufe (auch nach Neustart) senden nichts erneut.
    for _ in range(3):
        sender(transport).run_cycle()
    assert len(transport.sent) == 1


def test_confirmation_flow(accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    sender(FakeTransport()).run_cycle()
    t, = transmissions()
    with get_sessionmaker()() as s:
        ops.confirm(s, t.id, ACTOR, "In DATEV Belegbilderservice sichtbar")
        s.commit()
    assert transmissions()[0].status == TransmissionStatus.confirmed
    with get_sessionmaker()() as s, pytest.raises(ops.OperationError):
        ops.confirm(s, t.id, ACTOR, "nochmal")


# --- Fehlerfälle -------------------------------------------------------------------


def test_12_smtp_connection_failure_is_retried_later(accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    transport = FakeTransport(SmtpResult(AttemptOutcome.temporary_failure, error="Verbindung: ConnectionRefusedError"))
    s = sender(transport)
    s.run_cycle()
    t, = transmissions()
    assert t.status == TransmissionStatus.ready and t.next_attempt_at > datetime.now(UTC)
    s.run_cycle()
    assert len(transport.sent) == 1, "Backoff wird eingehalten"
    make_due(t.id)
    s.run_cycle()
    t, = transmissions()
    assert t.status == TransmissionStatus.sent_unconfirmed and t.attempts == 2
    assert [a.outcome for a in attempts(t.id)] == [AttemptOutcome.temporary_failure, AttemptOutcome.accepted]


def test_12_repeated_temporary_failures_end_in_failed(accounts, monkeypatch):
    configure_destination(monkeypatch)
    with get_sessionmaker()() as s:
        app_settings.set_setting(s, "datev.max_attempts", 2)
        s.commit()
    ready_invoice(accounts)
    temp = SmtpResult(AttemptOutcome.temporary_failure, 421, "4.7.0 try later")
    transport = FakeTransport(temp, temp)
    s = sender(transport)
    s.run_cycle()
    make_due(transmissions()[0].id)
    s.run_cycle()
    assert transmissions()[0].status == TransmissionStatus.failed


def test_permanent_rejection_is_not_retried(accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    transport = FakeTransport(SmtpResult(AttemptOutcome.rejected, 550, "5.1.1 mailbox unavailable"))
    sender(transport).run_cycle()
    sender(transport).run_cycle()
    assert len(transport.sent) == 1 and transmissions()[0].status == TransmissionStatus.failed


def test_13_unclear_smtp_result_blocks_automatic_resend(accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    transport = FakeTransport(SmtpResult(AttemptOutcome.unknown, error="Abbruch während DATA: SMTPServerDisconnected"))
    s = sender(transport)
    s.run_cycle()
    t, = transmissions()
    assert t.status == TransmissionStatus.manual_review and "unklar" in t.last_error
    for _ in range(3):
        s.run_cycle()
    assert len(transport.sent) == 1, "kein automatischer Zweitversand"
    with get_sessionmaker()() as db:  # Sendewunsch wird abgewiesen, solange ungeklärt
        with pytest.raises(ops.OperationError):
            ops.request_send(db, t.id, ACTOR, real_send_settings(), mode="manual")
    # Prüfer hat in DATEV nachgesehen: nicht angekommen → erneut freigegeben.
    with get_sessionmaker()() as db:
        ops.resolve_unclear(db, t.id, ACTOR, was_sent=False, reason="In DATEV nicht vorhanden")
        db.commit()
    s.run_cycle()
    assert len(transport.sent) == 2 and transmissions()[0].status == TransmissionStatus.sent_unconfirmed


def test_13_unclear_resolved_as_delivered(accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    s = sender(FakeTransport(SmtpResult(AttemptOutcome.unknown, error="timeout")))
    s.run_cycle()
    with get_sessionmaker()() as db:
        ops.resolve_unclear(db, transmissions()[0].id, ACTOR, was_sent=True, reason="In DATEV vorhanden")
        db.commit()
    assert transmissions()[0].status == TransmissionStatus.sent_unconfirmed
    assert documents()[0][0].status == DocumentStatus.transmitted


def test_11_worker_crash_during_sending_is_never_resent(accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    s = sender(FakeTransport())
    s.plan()
    s.promote()
    t, = transmissions()
    with get_sessionmaker()() as db:  # Worker hat "sending" gesetzt und ist dann abgestürzt
        db.execute(update(DatevTransmission).where(DatevTransmission.id == t.id).values(
            status=TransmissionStatus.sending, attempts=1, sending_started_at=datetime.now(UTC) - timedelta(hours=1)))
        db.add(DatevTransmissionAttempt(company_id=t.company_id, transmission_id=t.id, attempt_no=1,
                                        started_at=datetime.now(UTC) - timedelta(hours=1), test_mode=False))
        db.commit()
    transport = FakeTransport()
    result = sender(transport, worker="neuer-worker").run_cycle()
    assert result.recovered == 1 and transport.sent == []
    t, = transmissions()
    assert t.status == TransmissionStatus.manual_review
    assert attempts(t.id)[0].outcome == AttemptOutcome.unknown


def test_missing_smtp_secret_fails_before_sending(accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    monkeypatch.delenv(SECRET)
    transport = FakeTransport()
    sender(transport).run_cycle()
    assert transport.sent == [] and transmissions()[0].status == TransmissionStatus.pending


def test_revoked_release_stops_queued_send(accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    s = sender(FakeTransport())
    s.plan()
    s.promote()
    assert transmissions()[0].status == TransmissionStatus.ready
    with get_sessionmaker()() as db:  # Testmodus wieder eingeschaltet, bevor der Worker sendet
        app_settings.set_setting(db, "datev.test_mode", True)
        db.commit()
    transport = FakeTransport()
    sender(transport).send_next()
    assert transport.sent == [] and transmissions()[0].status == TransmissionStatus.pending


# --- Dubletten auf Versandebene ----------------------------------------------------


def test_3_duplicate_from_second_mailbox_is_never_sent(accounts, monkeypatch):
    configure_destination(monkeypatch)
    pdf = make_pdf(invoice_lines())
    deliver(accounts, [("r.pdf", pdf, PDF)], mailbox=INFO)
    deliver(accounts, [("r.pdf", pdf, PDF)], mailbox=INVOICE)
    process(accounts, INFO, INVOICE)
    transport = FakeTransport()
    sender(transport).run_cycle()
    assert len(transport.sent) == 1 and len(transmissions()) == 1


def test_database_blocks_second_live_transmission_for_same_invoice(accounts, monkeypatch):
    """Selbst wenn eine Dublette fälschlich auf 'ready' stünde, verhindert der Unique-Index den Zweitversand."""
    configure_destination(monkeypatch)
    pdf = make_pdf(invoice_lines())
    deliver(accounts, [("r.pdf", pdf, PDF)], mailbox=INFO)
    deliver(accounts, [("r.pdf", pdf, PDF)], mailbox=INVOICE)
    process(accounts, INFO, INVOICE)
    with get_sessionmaker()() as s:
        s.execute(update(Document).values(status=DocumentStatus.ready_for_datev))
        s.commit()
    transport = FakeTransport()
    sender(transport).run_cycle()
    assert len(transport.sent) == 1
    statuses = sorted(d.status.value for d, _ in documents())
    assert statuses == ["manual_review", "transmitted"]


@pytest.mark.skipif(int(os.environ.get("TEST_DB_POOL_SIZE", "1")) < 2,
                    reason="braucht echtes PostgreSQL mit mehreren Verbindungen (CI)")
def test_17_parallel_workers_send_once(accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    s = sender(FakeTransport())
    s.plan()
    s.promote()
    transport = FakeTransport()
    workers = [sender(transport, worker=f"w{i}") for i in range(4)]
    threads = [threading.Thread(target=w.send_next) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(transport.sent) == 1


# --- Testlieferung, ZUGFeRD, Testlauf -----------------------------------------------


def test_test_delivery_before_release(accounts, monkeypatch):
    configure_destination(monkeypatch, steps=("address_verified", "sender_authorized", "assignment_tested",
                                               "duplicate_check_tested"), release=False)
    ready_invoice(accounts)
    s = sender(FakeTransport())
    s.run_cycle()
    t, = transmissions()
    with get_sessionmaker()() as db:
        with pytest.raises(ops.OperationError, match="Produktivfreigabe"):
            ops.request_send(db, t.id, ACTOR, real_send_settings(), mode="manual")
    with get_sessionmaker()() as db:
        ops.request_send(db, t.id, ACTOR, real_send_settings(), mode="test_delivery")
        db.commit()
    transport = FakeTransport()
    sender(transport).run_cycle()
    assert len(transport.sent) == 1 and transmissions()[0].send_mode == "test_delivery"


def test_test_delivery_requires_checklist_steps(accounts, monkeypatch):
    configure_destination(monkeypatch, steps=("address_verified",), release=False)
    ready_invoice(accounts)
    sender(FakeTransport()).run_cycle()
    with get_sessionmaker()() as db, pytest.raises(ops.OperationError, match="Absender"):
        ops.request_send(db, transmissions()[0].id, ACTOR, real_send_settings(), mode="test_delivery")


def test_zugferd_held_until_format_confirmed(accounts, monkeypatch):
    configure_destination(monkeypatch)
    pdf = with_embedded_xml(make_pdf(invoice_lines("ZF-2026-77", gross="595,00", net="500,00", tax="95,00")), cii_xml())
    deliver(accounts, [("zf.pdf", pdf, PDF)])
    process(accounts)
    transport = FakeTransport()
    sender(transport).run_cycle()
    assert transport.sent == [] and "ZUGFeRD" in transmissions()[0].last_error
    with get_sessionmaker()() as db:
        app_settings.set_setting(db, "datev.upload_mail_zugferd_enabled", True)
        db.commit()
    sender(transport).run_cycle()
    assert len(transport.sent) == 1
    assert transport.sent[0][0].get_payload()[1].get_payload(decode=True) == pdf, "ZUGFeRD-PDF inkl. XML unverändert"


def test_dry_run_builds_mail_without_sending(accounts, monkeypatch):
    configure_destination(monkeypatch, test_mode=True)
    ready_invoice(accounts)
    doc, _ = documents()[0]
    with get_sessionmaker()() as db:
        t, preview = ops.dry_run(db, get_store(), doc.id, ACTOR)
        db.commit()
    assert preview["to"] == TARGET and preview["attachment"] == "RE-2026-1001.pdf" and preview["error"] is None
    with get_sessionmaker()() as db:
        row = db.get(DatevTransmission, t.id)
        assert row.test_mode and row.send_mode == "dry_run"
        assert db.scalar(select(func.count()).select_from(DatevTransmissionAttempt)
                         .where(DatevTransmissionAttempt.outcome == AttemptOutcome.test_mode)) == 1
    # Testläufe blockieren den späteren echten Versand nicht.
    configure_destination(monkeypatch)
    transport = FakeTransport()
    sender(transport).run_cycle()
    assert len(transport.sent) == 1


def test_destination_change_after_release_stops_sending(accounts, monkeypatch):
    configure_destination(monkeypatch)
    ready_invoice(accounts)
    from invoiceflow.services.datev_gate import set_target_address
    with get_sessionmaker()() as db:
        dest = db.scalar(select(DatevDestination).where(DatevDestination.company_id == company_id("adfunture", db)))
        set_target_address(db, dest, "11111111-2222-3333-4444-555555555555@uploadmail.datev.de", ACTOR)
        db.commit()
    transport = FakeTransport()
    sender(transport).run_cycle()
    assert transport.sent == []


# --- SmtpTransport-Klassifikation (ohne Netzwerk) --------------------------------


class FakeSMTP:
    behaviour: str = "ok"
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, context=None, timeout=None):
        if self.behaviour == "connect_fail":
            raise ConnectionRefusedError("refused")
        FakeSMTP.instances.append(self)
        self.calls = []

    def ehlo(self):
        return 250, b"ok"

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        if self.behaviour == "auth_fail":
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 auth failed")

    def mail(self, sender):
        return 250, b"ok"

    def rcpt(self, recipient):
        return (450, b"4.2.0 busy") if self.behaviour == "rcpt_busy" else (250, b"ok")

    def data(self, msg):
        if self.behaviour == "disconnect_in_data":
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")
        if self.behaviour == "data_refused":
            raise smtplib.SMTPDataError(554, b"no")
        return 250, b"2.0.0 queued as ABC"

    def rset(self):
        pass

    def quit(self):
        pass

    def close(self):
        pass


@pytest.mark.parametrize("behaviour,expected", [
    ("ok", AttemptOutcome.accepted),
    ("connect_fail", AttemptOutcome.temporary_failure),
    ("auth_fail", AttemptOutcome.rejected),
    ("rcpt_busy", AttemptOutcome.temporary_failure),
    ("data_refused", AttemptOutcome.rejected),
    ("disconnect_in_data", AttemptOutcome.unknown),
])
def test_smtp_result_classification(monkeypatch, behaviour, expected):
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("APP_ENV", "development")  # nur für die Konstruktion; Netzwerk ist ersetzt
    FakeSMTP.behaviour = behaviour
    transport = SmtpTransport()
    msg = EmailMessage()
    msg["Subject"] = "x"
    msg.set_content("x")
    params = SmtpParams("smtp.strato.de", 465, TlsMode.ssl, "u", "p")
    assert transport.send(msg, params, "a@b.de", TARGET).outcome == expected


def test_wire_bytes_use_crlf_everywhere():
    """Strato lehnt nacktes LF ab: 554 5.6.0 "A header line must be terminated by CRLF"."""
    import re

    from invoiceflow.datev.smtp import to_wire_bytes

    msg = EmailMessage()
    msg["From"] = "info@adfunture.de"
    msg["To"] = "x@uploadmail.datev.de"
    msg["Subject"] = "Beleg: " + "Sehr langer Lieferantenname GmbH & Co. KG " * 4  # wird gefaltet
    msg.set_content("Beleg automatisch übermittelt.\nZweite Zeile\n")
    msg.add_attachment(make_pdf(invoice_lines()), maintype="application", subtype="pdf", filename="rechnung.pdf")
    wire = to_wire_bytes(msg)
    assert re.search(rb"(?<!\r)\n", wire) is None, "kein LF ohne vorangehendes CR"
    assert wire.count(b"\r\n") > 20
