"""IMAP-Zugriff, ausschließlich lesend.

* Ordner werden mit ``readonly=True`` (EXAMINE) geöffnet, Nachrichten mit
  ``BODY.PEEK[]`` geladen: Es werden keine Flags gesetzt, nichts gelöscht
  oder verschoben.
* TLS mit Zertifikatsprüfung (implizit 993 oder STARTTLS).
"""
from __future__ import annotations

import socket
import ssl
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from invoiceflow.db.enums import TlsMode


class MailServerError(RuntimeError):
    """Verbindungs-, Login- oder Protokollfehler. Nachricht enthält keine Zugangsdaten."""


@dataclass(frozen=True)
class FolderInfo:
    uidvalidity: int
    uidnext: int | None
    exists: int


@dataclass(frozen=True)
class MessageMeta:
    uid: int
    internaldate: datetime  # zeitzonenbewusst
    size: int


class MailServer(Protocol):
    def list_folders(self) -> list[str]: ...
    def select_folder(self, name: str) -> FolderInfo: ...
    def search_since(self, day: date) -> list[int]: ...
    def search_uid_from(self, first_uid: int) -> list[int]: ...
    def fetch_meta(self, uids: Iterable[int]) -> dict[int, MessageMeta]: ...
    def fetch_raw(self, uid: int) -> bytes: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class ConnectionParams:
    host: str
    port: int
    tls_mode: TlsMode
    username: str
    password: str
    timeout: float = 60.0


MailServerFactory = Callable[[ConnectionParams], MailServer]


class ImapClientServer:
    """MailServer über IMAPClient."""

    def __init__(self, params: ConnectionParams) -> None:
        from imapclient import IMAPClient
        from imapclient.exceptions import IMAPClientError

        self._errors = (IMAPClientError, OSError, ssl.SSLError, socket.timeout)
        context = ssl.create_default_context()
        try:
            if params.tls_mode == TlsMode.ssl:
                self.client = IMAPClient(params.host, port=params.port, ssl=True, ssl_context=context, timeout=params.timeout)
            else:
                self.client = IMAPClient(params.host, port=params.port, ssl=False, timeout=params.timeout)
                self.client.starttls(context)
            # INTERNALDATE mit Zeitzone liefern statt in lokale Zeit umzurechnen.
            self.client.normalise_times = False
            self.client.login(params.username, params.password)
        except self._errors as exc:
            raise MailServerError(_describe(exc, params.host)) from None

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except self._errors as exc:
            raise MailServerError(_describe(exc)) from None

    def list_folders(self) -> list[str]:
        return [name for _flags, _delim, name in self._call(self.client.list_folders)]

    def select_folder(self, name: str) -> FolderInfo:
        info = self._call(self.client.select_folder, name, readonly=True)
        return FolderInfo(
            uidvalidity=int(info[b"UIDVALIDITY"]),
            uidnext=int(info[b"UIDNEXT"]) if b"UIDNEXT" in info else None,
            exists=int(info.get(b"EXISTS", 0)),
        )

    def search_since(self, day: date) -> list[int]:
        return sorted(self._call(self.client.search, ["SINCE", day]))

    def search_uid_from(self, first_uid: int) -> list[int]:
        # "n:*" liefert immer mindestens die höchste UID, auch wenn sie < n ist.
        return sorted(u for u in self._call(self.client.search, ["UID", f"{first_uid}:*"]) if u >= first_uid)

    def fetch_meta(self, uids: Iterable[int]) -> dict[int, MessageMeta]:
        uids = list(uids)
        if not uids:
            return {}
        data = self._call(self.client.fetch, uids, ["INTERNALDATE", "RFC822.SIZE"])
        return {
            uid: MessageMeta(uid=uid, internaldate=item[b"INTERNALDATE"], size=int(item[b"RFC822.SIZE"]))
            for uid, item in data.items()
        }

    def fetch_raw(self, uid: int) -> bytes:
        data = self._call(self.client.fetch, [uid], ["BODY.PEEK[]"])
        if uid not in data or b"BODY[]" not in data[uid]:
            raise MailServerError(f"Nachricht UID {uid} nicht mehr vorhanden")
        return bytes(data[uid][b"BODY[]"])

    def close(self) -> None:
        try:
            self.client.logout()
        except Exception:  # noqa: BLE001 – Abmelden ist best effort
            pass


def _describe(exc: BaseException, host: str | None = None) -> str:
    text = str(exc) or type(exc).__name__
    # Server-Antworten können den Benutzernamen enthalten, nie das Passwort; trotzdem kürzen.
    prefix = f"{host}: " if host else ""
    return f"{prefix}{type(exc).__name__}: {text[:300]}"


def default_factory(params: ConnectionParams) -> MailServer:
    return ImapClientServer(params)
