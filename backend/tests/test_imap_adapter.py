"""Der IMAPClient-Adapter darf Postfächer nicht verändern."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from invoiceflow.db.enums import TlsMode
from invoiceflow.mail import imap
from invoiceflow.mail.imap import ConnectionParams, ImapClientServer, MailServerError


class RecordingClient:
    instances: list["RecordingClient"] = []

    def __init__(self, host, port=None, ssl=True, ssl_context=None, timeout=None):
        self.calls: list[tuple] = [("init", host, port, ssl, ssl_context is not None)]
        RecordingClient.instances.append(self)
        if host == "down.example":
            raise OSError("Network unreachable")

    def __getattr__(self, name):
        if name in {"delete_messages", "expunge", "move", "copy", "add_flags", "set_flags", "remove_flags", "append"}:
            raise AssertionError(f"verändernder IMAP-Befehl aufgerufen: {name}")
        raise AttributeError(name)

    def starttls(self, ctx):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user))

    def select_folder(self, name, readonly=False):
        self.calls.append(("select", name, readonly))
        return {b"UIDVALIDITY": 77, b"UIDNEXT": 10, b"EXISTS": 3}

    def search(self, criteria):
        self.calls.append(("search", criteria))
        return [9, 4, 7]

    def fetch(self, uids, items):
        self.calls.append(("fetch", list(uids), items))
        if items == ["BODY.PEEK[]"]:
            return {uids[0]: {b"BODY[]": b"raw"}}
        return {u: {b"INTERNALDATE": datetime(2026, 11, 9, tzinfo=UTC), b"RFC822.SIZE": 100} for u in uids}

    def list_folders(self):
        return [((), b"/", "INBOX"), ((), b"/", "Rechnungen")]

    def logout(self):
        self.calls.append(("logout",))


@pytest.fixture(autouse=True)
def fake_imapclient(monkeypatch):
    import imapclient

    RecordingClient.instances.clear()
    monkeypatch.setattr(imapclient, "IMAPClient", RecordingClient)


def params(host="imap.strato.de", tls=TlsMode.ssl):
    return ConnectionParams(host=host, port=993, tls_mode=tls, username="info@adfunture.de", password="geheim")


def test_folder_opened_readonly_and_body_peeked():
    server = ImapClientServer(params())
    assert server.select_folder("INBOX").uidvalidity == 77
    assert server.fetch_raw(4) == b"raw"
    client = RecordingClient.instances[0]
    assert ("select", "INBOX", True) in client.calls
    assert ("fetch", [4], ["BODY.PEEK[]"]) in client.calls
    assert client.normalise_times is False


def test_uid_range_filters_star_quirk():
    server = ImapClientServer(params())
    server.select_folder("INBOX")
    assert server.search_uid_from(8) == [9]


def test_starttls_mode():
    ImapClientServer(params(tls=TlsMode.starttls))
    client = RecordingClient.instances[0]
    assert client.calls[0][3] is False and ("starttls",) in client.calls


def test_connection_error_is_wrapped_without_password():
    with pytest.raises(MailServerError) as err:
        ImapClientServer(params(host="down.example"))
    assert "geheim" not in str(err.value)


def test_default_factory_uses_adapter():
    assert isinstance(imap.default_factory(params()), ImapClientServer)
