"""Alle Ordner und Unterordner eines Postfachs werden überwacht – außer Gesendet, Entwürfe, Papierkorb, Spam."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from invoiceflow.db.enums import DocumentStatus
from invoiceflow.db.models import AuditLog, Company, EmailMessage, MailboxSyncState
from invoiceflow.db.session import get_sessionmaker
from invoiceflow.mail.folders import exclusion_reason
from invoiceflow.mail.imap import FolderListing

from .fake_imap import FakeFolder, build_email
from .samples import invoice_lines, make_pdf
from .test_imap_sync import INFO, ON_START, accounts, mailbox_id, messages, sync  # noqa: F401 – Fixture
from .test_pipeline import PDF, documents, processor


def _folders() -> dict[str, MailboxSyncState]:
    with get_sessionmaker()() as s:
        return {f.folder_name: f for f in s.scalars(select(MailboxSyncState).where(
            MailboxSyncState.mailbox_id == mailbox_id(INFO)))}


def _setup_tree(account) -> None:
    for name in ("Rechnungen", "Rechnungen/2026", "Lieferanten/Thule/Belege", "Archiv", "Gesendet", "Gesendet/2026",
                 "Ausgang", "Ausgang/Alt", "Papierkorb", "Spamverdacht", "Entwürfe", "INBOX/Steuerberater"):
        account.folders[name] = FakeFolder()
    account.folder_flags.update({"Ausgang": {r"\Sent"}, "Papierkorb": {r"\Trash"}, "Archiv": {r"\Archive"}})


@pytest.mark.parametrize("name,flags,excluded", [
    ("INBOX", set(), False),
    ("Rechnungen/2026", set(), False),
    ("Archiv", {r"\Archive"}, False),
    ("Gesendet", set(), True),
    ("Gesendete Objekte", set(), True),
    ("INBOX.Sent", set(), True),
    ("Sent/2026", set(), True),
    ("Ausgang", {r"\Sent"}, True),
    ("Junk-E-Mail", set(), True),
    ("Posteingang", {r"\Junk"}, True),
    ("Entwürfe", set(), True),
    ("Gelöschte Elemente", set(), True),
    ("[Gmail]/Alle Nachrichten", {r"\All"}, True),
])
def test_exclusion_rules(name, flags, excluded):
    delimiter = "." if name.startswith("INBOX.") else "/"
    assert (exclusion_reason(FolderListing(name, frozenset(flags), delimiter)) is not None) is excluded


def test_noselect_folders_are_not_selectable():
    assert not FolderListing("[Gmail]", frozenset({r"\Noselect", r"\HasChildren"})).selectable


def test_all_subfolders_are_discovered_and_synced(accounts):
    acc = accounts[INFO]
    _setup_tree(acc)
    acc.deliver(build_email("im Unterordner", body="u1"), ON_START + timedelta(hours=1), folder="Lieferanten/Thule/Belege")
    acc.deliver(build_email("im INBOX-Unterordner", body="u2"), ON_START + timedelta(hours=2), folder="INBOX/Steuerberater")
    acc.deliver(build_email("Ausgangsrechnung", body="out"), ON_START + timedelta(hours=3), folder="Gesendet")
    acc.deliver(build_email("gelöscht", body="trash"), ON_START + timedelta(hours=4), folder="Papierkorb")
    acc.deliver(build_email("alt", body="alt"), ON_START - timedelta(days=3), folder="Archiv")

    result = sync(accounts)
    assert result.status == "ok"
    assert {m.subject for m in messages()} == {"im Unterordner", "im INBOX-Unterordner"}

    folders = _folders()
    monitored = {n for n, f in folders.items() if f.is_enabled}
    assert {"INBOX", "Rechnungen", "Rechnungen/2026", "Lieferanten/Thule/Belege", "Archiv", "INBOX/Steuerberater"} <= monitored
    excluded = {n: f.exclusion_reason for n, f in folders.items() if not f.is_enabled}
    assert set(excluded) == {"Gesendet", "Gesendet/2026", "Ausgang", "Ausgang/Alt", "Papierkorb", "Spamverdacht", "Entwürfe"}
    assert "Ausgangsrechnungen" in excluded["Ausgang/Alt"], "Unterordner erben den Ausschluss"
    with get_sessionmaker()() as s:
        log = s.scalar(select(AuditLog).where(AuditLog.action == "mailbox.folders_discovered"))
    assert "Gesendet" in log.details["excluded"]


def test_new_folder_later_is_picked_up_with_backfill(accounts):
    sync(accounts)
    acc = accounts[INFO]
    acc.folders["Neu angelegt"] = FakeFolder()
    acc.deliver(build_email("vor dem Anlegen eingegangen", body="x"), ON_START + timedelta(hours=1), folder="Neu angelegt")
    assert sync(accounts).discovered == ["Neu angelegt"]
    assert [m.subject for m in messages()] == ["vor dem Anlegen eingegangen"]


def test_admin_choice_is_never_overwritten(accounts):
    acc = accounts[INFO]
    acc.folders["Spam"] = FakeFolder()
    acc.deliver(build_email("echte Rechnung im Spam", body="s"), ON_START + timedelta(hours=1), folder="Spam")
    sync(accounts)
    assert messages() == []
    with get_sessionmaker()() as s:  # Admin schaltet den Spam-Ordner bewusst ein
        s.execute(update(MailboxSyncState).where(MailboxSyncState.folder_name == "Spam").values(is_enabled=True))
        s.execute(update(MailboxSyncState).where(MailboxSyncState.folder_name == "INBOX").values(is_enabled=False))
        s.commit()
    sync(accounts)
    folders = _folders()
    assert folders["Spam"].is_enabled and not folders["INBOX"].is_enabled
    assert [m.subject for m in messages()] == ["echte Rechnung im Spam"]


def test_moved_message_is_not_imported_twice(accounts):
    acc = accounts[INFO]
    acc.folders["Rechnungen"] = FakeFolder()
    uid = acc.deliver(build_email("Rechnung", body="einmal"), ON_START + timedelta(hours=1))
    sync(accounts)
    acc.move(uid, "INBOX", "Rechnungen")  # Nutzer sortiert die Mail ein
    sync(accounts)
    assert len(messages()) == 1


def test_vanished_folder_is_marked_and_comes_back(accounts):
    acc = accounts[INFO]
    acc.folders["Temp"] = FakeFolder()
    sync(accounts)
    temp = acc.folders.pop("Temp")
    sync(accounts)
    assert _folders()["Temp"].missing_since is not None
    acc.folders["Temp"] = temp
    sync(accounts)
    assert _folders()["Temp"].missing_since is None


# --- Schutz gegen eigene Ausgangsrechnungen ------------------------------------------


def _set_own_vat(vat: str) -> None:
    with get_sessionmaker()() as s:
        s.execute(update(Company).where(Company.slug == "adfunture").values(vat_id=vat))
        s.commit()


def _process_pdf(accounts, lines, folder="INBOX"):
    accounts[INFO].deliver(build_email("Rechnung", attachments=[("r.pdf", make_pdf(lines), PDF)], body="x"),
                           ON_START + timedelta(hours=1), folder=folder)
    sync(accounts)
    with get_sessionmaker()() as s:
        msg_id = s.scalar(select(EmailMessage.id))
    processor().process_message(msg_id)
    return documents()[0][0]


def test_own_outgoing_invoice_is_held(accounts):
    """Liegt eine eigene Ausgangsrechnung z. B. in einem Kundenordner, darf sie nicht als Eingangsrechnung an DATEV."""
    _set_own_vat("DE300300300")
    lines = invoice_lines(supplier="Adfunture Outdoor GmbH", recipient="Kunde Müller GmbH", vat="DE300300300")
    accounts[INFO].folders["Kunden/Müller"] = FakeFolder()
    doc = _process_pdf(accounts, lines, folder="Kunden/Müller")
    assert doc.status == DocumentStatus.manual_review and "Ausgangsrechnung" in doc.review_reason


def test_own_vat_as_customer_reference_is_fine(accounts):
    _set_own_vat("DE300300300")
    lines = invoice_lines() + ["Ihre USt-ID: DE300300300"]
    doc = _process_pdf(accounts, lines)
    assert doc.status == DocumentStatus.ready_for_datev, doc.review_reason
