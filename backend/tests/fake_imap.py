"""In-Memory-IMAP-Server für Tests der Abruflogik (implementiert das MailServer-Protokoll).

Bildet die für InvoiceFlow relevanten IMAP-Eigenschaften nach: UIDs je Ordner,
UIDVALIDITY, SEARCH SINCE nach Datum (Serverzeit UTC), INTERNALDATE, Größe.
Fehler lassen sich gezielt einspeisen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.message import EmailMessage
from email.utils import format_datetime

from invoiceflow.mail.imap import ConnectionParams, FolderInfo, FolderListing, MailServerError, MessageMeta

PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def pdf(label: str) -> bytes:
    return PDF.replace(b"1 0 obj", f"% {label}\n1 0 obj".encode())


def build_email(
    subject: str = "Ihre Rechnung",
    *,
    sender: str = "buchhaltung@lieferant.example",
    to: str = "invoice@adfunture.de",
    date_header: datetime | None = None,
    body: str = "Anbei die Rechnung.",
    attachments: list[tuple[str, bytes, str]] | None = None,
    message_id: str | None = None,
    auth_results: list[str] | None = None,
) -> bytes:
    msg = EmailMessage()
    for value in auth_results or []:  # oberster Header zuerst (wie vom empfangenden Server gesetzt)
        msg["Authentication-Results"] = value
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = format_datetime(date_header or datetime(2026, 11, 9, 9, 0, tzinfo=UTC))
    msg["Message-ID"] = message_id or f"<{abs(hash((subject, body)))}@lieferant.example>"
    msg.set_content(body)
    if attachments is None:  # Mails ohne Anhang und Link werden nicht importiert; Standard ist eine Rechnung.
        attachments = [("rechnung.pdf", pdf(f"{subject}|{body}"), "application/pdf")]
    for filename, data, mime in attachments:
        maintype, subtype = mime.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()


@dataclass
class FakeFolder:
    uidvalidity: int = 1000
    next_uid: int = 1
    messages: dict[int, tuple[datetime, bytes]] = field(default_factory=dict)


@dataclass
class FakeAccount:
    password: str = "richtig"
    folders: dict[str, FakeFolder] = field(default_factory=lambda: {"INBOX": FakeFolder()})
    folder_flags: dict[str, set[str]] = field(default_factory=dict)  # z. B. {"Gesendet": {"\Sent"}}
    fail_connects: int = 0  # so viele Verbindungsversuche schlagen fehl
    fail_fetch_uids: set[int] = field(default_factory=set)
    connects: int = 0
    raw_fetches: list[int] = field(default_factory=list)

    def deliver(self, raw: bytes, internaldate: datetime, folder: str = "INBOX") -> int:
        f = self.folders.setdefault(folder, FakeFolder())
        uid = f.next_uid
        f.messages[uid] = (internaldate, raw)
        f.next_uid += 1
        return uid

    def move(self, uid: int, source: str, target: str) -> int:
        """Wie ein Mailprogramm: Nachricht verschieben = neue UID im Zielordner."""
        internaldate, raw = self.folders[source].messages.pop(uid)
        return self.deliver(raw, internaldate, target)

    def renumber(self, folder: str = "INBOX", new_uidvalidity: int = 2000) -> None:
        """Simuliert UIDVALIDITY-Wechsel (z. B. Server-Migration): alle UIDs neu."""
        f = self.folders[folder]
        old = [f.messages[u] for u in sorted(f.messages)]
        f.uidvalidity, f.messages, f.next_uid = new_uidvalidity, {}, 1
        for internaldate, raw in old:
            self.deliver(raw, internaldate, folder)


class FakeMailServer:
    def __init__(self, account: FakeAccount, params: ConnectionParams) -> None:
        account.connects += 1
        if account.fail_connects > 0:
            account.fail_connects -= 1
            raise MailServerError(f"{params.host}: ConnectionRefusedError: Verbindung abgelehnt")
        if params.password != account.password:
            raise MailServerError(f"{params.host}: LoginError: authentication failed")
        self.account = account
        self.selected: FakeFolder | None = None
        self.closed = False

    def list_folders(self) -> list[FolderListing]:
        return [FolderListing(name, frozenset(self.account.folder_flags.get(name, set())), "/")
                for name in self.account.folders]

    def select_folder(self, name: str) -> FolderInfo:
        if name not in self.account.folders:
            raise MailServerError(f"Ordner {name} existiert nicht")
        self.selected = self.account.folders[name]
        return FolderInfo(self.selected.uidvalidity, self.selected.next_uid, len(self.selected.messages))

    def search_since(self, day: date) -> list[int]:
        return sorted(u for u, (d, _) in self.selected.messages.items() if d.astimezone(UTC).date() >= day)

    def search_uid_from(self, first_uid: int) -> list[int]:
        return sorted(u for u in self.selected.messages if u >= first_uid)

    def fetch_meta(self, uids) -> dict[int, MessageMeta]:
        return {u: MessageMeta(u, self.selected.messages[u][0], len(self.selected.messages[u][1]))
                for u in uids if u in self.selected.messages}

    def fetch_raw(self, uid: int) -> bytes:
        if uid in self.account.fail_fetch_uids:
            raise MailServerError("Verbindung während FETCH abgebrochen")
        self.account.raw_fetches.append(uid)
        return self.selected.messages[uid][1]

    def close(self) -> None:
        self.closed = True


def factory_for(accounts: dict[str, FakeAccount]):
    """Ordnet Verbindungen anhand des IMAP-Benutzernamens einem Fake-Konto zu."""
    def factory(params: ConnectionParams) -> FakeMailServer:
        return FakeMailServer(accounts[params.username], params)

    return factory
