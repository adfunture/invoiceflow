"""MIME-Zerlegung eingehender Nachrichten.

Der Inhalt ist nicht vertrauenswürdig: Dateinamen werden bereinigt, Typen
am Inhalt erkannt (nicht am Header), Größen begrenzt. Weitergeleitete
Nachrichten (message/rfc822) werden durchsucht, damit Rechnungen in
Weiterleitungen nicht verloren gehen.
"""
from __future__ import annotations

import base64
import binascii
import quopri
import re
import unicodedata
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from invoiceflow.db.enums import AttachmentSafety

MAX_PREVIEW_CHARS = 2000
MAX_NESTING = 10  # verschachtelte Weiterleitungen; schützt vor Rekursionsbomben

# Inhaltserkennung (Magic Bytes). Reihenfolge ist relevant.
_SIGNATURES: list[tuple[bytes, str]] = [
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"PK\x03\x04", "application/zip"),
    (b"MZ", "application/x-msdownload"),
    (b"\x7fELF", "application/x-executable"),
    (b"\xd0\xcf\x11\xe0", "application/x-ole-storage"),  # altes Office, oft Makros
]
_ALLOWED_TYPES = {"application/pdf", "application/xml", "image/png", "image/jpeg", "image/tiff"}
_BLOCKED_EXTENSIONS = {
    "exe", "com", "bat", "cmd", "scr", "pif", "js", "jse", "vbs", "vbe", "wsf", "ps1", "msi", "dll", "jar",
    "lnk", "hta", "iso", "img", "docm", "xlsm", "pptm", "html", "htm", "svg",
}
EXECUTABLE_TYPES = {"application/x-msdownload", "application/x-executable", "application/x-ole-storage"}
# Office-Dateien sind technisch ZIP-Archive (Angebotslisten, Preislisten …), aber nie ein Rechnungsformat.
_OFFICE_EXTENSIONS = {"xlsx", "docx", "pptx", "ods", "odt", "odp"}
# Alte Office-Formate (OLE): Listen wie "Sendungsnummern.xls" – kein Rechnungsformat und für uns
# ungefährlich, weil nie geöffnet. Andere OLE-Dateien (unbekannte Endung) bleiben verdächtig.
_LEGACY_OFFICE_EXTENSIONS = {"xls", "doc", "ppt"}


def _ext(filename: str | None) -> str:
    return filename.rsplit(".", 1)[1].lower() if filename and "." in filename else ""


def is_dangerous(filename: str | None, detected: str | None) -> bool:
    """Ausführbarer oder makrofähiger Anhang, der als Warnung in die Prüfung gehört."""
    ext = _ext(filename)
    if detected == "application/x-ole-storage" and ext in _LEGACY_OFFICE_EXTENSIONS:
        return False
    return detected in EXECUTABLE_TYPES or ext in _BLOCKED_EXTENSIONS
# Outlook benennt eingebettete Bilder (Signatur, Logo, eingefügte Screenshots) image001.png usw.
_OUTLOOK_EMBEDDED = re.compile(r"^image\d{3}\.(png|jpe?g|gif)$", re.I)
_INVOICE_WORDS = re.compile(
    r"\b(rechnung|rechnungs-?nr|invoice|gutschrift|credit\s*note|storno|faktura|zahlungsbeleg|beleg)\w*", re.I
)
_URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.I)
_LINK_HINT = re.compile(r"invoice|rechnung|billing|bill|beleg|download|receipt|quittung|\.pdf", re.I)
# Links, die nie zu einer Rechnung führen: soziale Netzwerke, Hilfe, Rechtliches, Abmelden, App-Stores, Bilder.
_NOISE_HOSTS = re.compile(
    r"(^|\.)(twitter\.com|x\.com|facebook\.com|fb\.com|instagram\.com|linkedin\.com|youtube\.com|youtu\.be|"
    r"xing\.com|tiktok\.com|pinterest\.[a-z.]+|apps\.apple\.com|play\.google\.com|itunes\.apple\.com)$", re.I)
_NOISE_PATH = re.compile(
    r"(^|[/_.\-])(help|hilfe|smarthelp|support|faq|kontakt|contact|privacy|datenschutz|impressum|imprint|legal|agb|"
    r"terms|nutzungsbedingungen|unsubscribe|abmelden|abbestellen|newsletter|preferences|einstellungen|settings|"
    r"safety|security|sicherheit|mobile-apps|apps|social)([/_.\-]|$)", re.I)
_NOISE_FILE = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js|ico)$", re.I)
_STRONG_HINT = re.compile(r"invoice|rechnung|receipt|beleg|quittung|\.pdf", re.I)


def _is_noise_link(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    host, path = (parsed.hostname or "").lower(), parsed.path or "/"
    if _NOISE_HOSTS.search(host) or _NOISE_FILE.search(path):
        return True
    return bool(_NOISE_PATH.search(path)) and not _STRONG_HINT.search(path)


@dataclass
class ParsedAttachment:
    part_index: int
    filename: str | None
    declared_content_type: str | None
    detected_content_type: str | None
    data: bytes | None
    size_bytes: int
    is_inline: bool
    content_id: str | None
    safety: AttachmentSafety
    rejection_reason: str | None = None

    @property
    def extension(self) -> str:
        if self.filename and "." in self.filename:
            return self.filename.rsplit(".", 1)[1].lower()
        return {"application/pdf": "pdf", "application/xml": "xml"}.get(self.detected_content_type or "", "bin")

    @property
    def needs_processing(self) -> bool:
        """Verwertbar oder prüfwürdig (kaputt, zu groß, gefährlich, ZIP) – sonst ohne Belang."""
        if self.safety in (AttachmentSafety.ok, AttachmentSafety.corrupt, AttachmentSafety.rejected_size):
            return True
        if self.safety == AttachmentSafety.rejected_type:
            return is_dangerous(self.filename, self.detected_content_type) or _ext(self.filename) == "zip"
        return False


@dataclass
class ParsedMessage:
    message_id: str | None
    subject: str | None
    from_address: str | None
    to_addresses: list[str]
    header_date: datetime | None
    text_body: str
    attachments: list[ParsedAttachment] = field(default_factory=list)
    invoice_links: list[str] = field(default_factory=list)
    body_mentions_invoice: bool = False
    parse_warnings: list[str] = field(default_factory=list)
    reply_references: list[str] = field(default_factory=list)  # In-Reply-To und References

    @property
    def needs_processing(self) -> bool:
        """Nur Mails mit Anhang oder Rechnungslink werden importiert; alles andere ist keine Rechnungspost."""
        return bool(self.invoice_links) or any(a.needs_processing for a in self.attachments)

    @property
    def body_preview(self) -> str | None:
        text = re.sub(r"\s+", " ", self.text_body).strip()
        return text[:MAX_PREVIEW_CHARS] or None


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hrefs: list[str] = []
        self.cids: set[str] = set()
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)
        if tag == "img":
            src = (dict(attrs).get("src") or "").strip()
            if src.lower().startswith("cid:"):
                self.cids.add(src[4:].strip("<>").lower())
        if tag in ("br", "p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def sanitize_filename(name: str | None) -> str | None:
    if not name:
        return None
    name = unicodedata.normalize("NFC", str(name))
    name = name.replace("\\", "/").rsplit("/", 1)[-1]  # keine Pfade
    name = "".join(c for c in name if unicodedata.category(c)[0] != "C")  # keine Steuerzeichen
    name = name.strip(" .")
    return name[:255] or None


def detect_content_type(data: bytes) -> str | None:
    for sig, mime in _SIGNATURES:
        if data.startswith(sig):
            return mime
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"<?xml") or re.match(rb"<([A-Za-z_][\w.-]*:)?(Invoice|CreditNote|CrossIndustryInvoice)\b", head):
        return "application/xml"
    return None


def classify_attachment(filename: str | None, data: bytes, max_bytes: int) -> tuple[AttachmentSafety, str | None, str | None]:
    """→ (Status, erkannter Typ, Grund)."""
    detected = detect_content_type(data)
    if len(data) > max_bytes:
        return AttachmentSafety.rejected_size, detected, f"Anhang größer als {max_bytes // (1024 * 1024)} MB"
    ext = filename.rsplit(".", 1)[1].lower() if filename and "." in filename else ""
    if ext in _BLOCKED_EXTENSIONS:
        return AttachmentSafety.rejected_type, detected, f"Dateityp .{ext} wird nicht verarbeitet"
    if detected == "application/x-ole-storage" and ext in _LEGACY_OFFICE_EXTENSIONS:
        return AttachmentSafety.rejected_type, detected, f"Office-Datei (.{ext}) ist kein Rechnungsformat"
    if detected in EXECUTABLE_TYPES:
        return AttachmentSafety.rejected_type, detected, f"Inhalt ist {detected}"
    if detected == "application/zip":
        if ext in _OFFICE_EXTENSIONS:
            return AttachmentSafety.rejected_type, detected, f"Office-Datei (.{ext}) ist kein Rechnungsformat"
        return AttachmentSafety.rejected_type, detected, "ZIP-Archiv wird nicht entpackt – bitte manuell prüfen"
    if detected is None:
        if ext in {"pdf", "xml"}:
            return AttachmentSafety.corrupt, None, f"Endung .{ext}, aber Inhalt passt nicht"
        return AttachmentSafety.rejected_type, None, "Unbekannter Dateityp"
    if detected == "application/pdf" and b"%%EOF" not in data[-2048:]:
        return AttachmentSafety.corrupt, detected, "PDF unvollständig (kein %%EOF)"
    if detected not in _ALLOWED_TYPES:
        return AttachmentSafety.rejected_type, detected, f"Typ {detected} nicht erlaubt"
    return AttachmentSafety.ok, detected, None


def _decode_header(msg: Message, name: str) -> str | None:
    try:
        value = msg.get(name)
        return None if value is None else str(value).strip() or None
    except Exception:  # noqa: BLE001 – kaputte Header dürfen den Import nicht stoppen
        raw = msg.get_all(name, [None])[0]
        return None if raw is None else str(raw)


def _addresses(msg: Message, *names: str) -> list[str]:
    values: list[str] = []
    for n in names:
        try:
            values += [str(v) for v in msg.get_all(n, [])]
        except Exception:  # noqa: BLE001
            continue
    return [addr.lower() for _, addr in getaddresses(values) if addr]


def _part_text(part: Message) -> str:
    try:
        return part.get_content() if isinstance(part, EmailMessage) else part.get_payload(decode=True).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 – unbekannter Zeichensatz
        payload = part.get_payload(decode=True) or b""
        return payload.decode("latin-1", "replace")


def _invoice_links(urls: list[str], text: str) -> list[str]:
    found: list[str] = []
    mentions = bool(_INVOICE_WORDS.search(text))
    for url in urls:
        url = url.strip().rstrip(".,;:!?")
        if not url.lower().startswith(("http://", "https://")) or len(url) > 2000:
            continue
        if _is_noise_link(url):
            continue
        if (_LINK_HINT.search(url) or mentions) and url not in found:
            found.append(url)
    return found[:50]


def _leaves(part: Message, warnings: list[str], depth: int = 0) -> Iterator[Message]:
    """Alle Blatt-Teile, inklusive Inhalt weitergeleiteter Nachrichten.

    Manche Mailprogramme kodieren message/rfc822 (regelwidrig) als base64;
    solche Teile werden dekodiert und erneut geparst.
    """
    if depth > MAX_NESTING:
        warnings.append("NestingTooDeep")
        return
    if part.get_content_type() == "message/rfc822":
        cte = str(part.get("Content-Transfer-Encoding", "")).lower().strip()
        if cte in ("base64", "quoted-printable"):
            payload = part.get_payload()
            inner = payload[0] if isinstance(payload, list) and payload else None
            encoded = inner.get_payload() if inner is not None and not inner.is_multipart() else ""
            try:
                data = (base64.b64decode(encoded) if cte == "base64" else quopri.decodestring(encoded.encode()))
                yield from _leaves(BytesParser(policy=policy.default).parsebytes(data), warnings, depth + 1)
            except (binascii.Error, ValueError, AttributeError):
                warnings.append("InvalidEncodedMessage")
            return
    if part.is_multipart():
        for sub in part.get_payload():
            yield from _leaves(sub, warnings, depth + 1)
        return
    yield part


def parse_message(raw: bytes, *, max_attachment_bytes: int) -> ParsedMessage:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    warnings: list[str] = [type(d).__name__ for d in msg.defects]

    header_date = None
    date_value = _decode_header(msg, "Date")
    if date_value:
        try:
            header_date = parsedate_to_datetime(date_value)
            if header_date.tzinfo is None:
                header_date = None  # ohne Zeitzone nicht eindeutig
        except (TypeError, ValueError):
            warnings.append("InvalidDateHeader")

    text_chunks: list[str] = []
    urls: list[str] = []
    attachments: list[ParsedAttachment] = []
    referenced_cids: set[str] = set()

    for index, part in enumerate(_leaves(msg, warnings), start=1):
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()
        try:
            raw_name = part.get_filename()
        except Exception:  # noqa: BLE001
            raw_name = None
            warnings.append(f"InvalidFilename(part {index})")
        filename = sanitize_filename(raw_name)
        is_body_text = content_type in ("text/plain", "text/html") and disposition != "attachment" and not filename

        if is_body_text:
            text = _part_text(part)
            if content_type == "text/html":
                parser = _HtmlText()
                try:
                    parser.feed(text)
                except Exception:  # noqa: BLE001
                    warnings.append("InvalidHtml")
                urls += parser.hrefs
                referenced_cids |= parser.cids
                text = "".join(parser.parts)
            text_chunks.append(text)
            urls += _URL_RE.findall(text)
            continue

        try:
            data = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001
            data = None
        content_id = part.get("Content-ID")
        is_inline = disposition == "inline" or (disposition is None and content_id is not None and not filename)
        if data is None:
            attachments.append(ParsedAttachment(index, filename, content_type, None, None, 0, is_inline,
                                                content_id, AttachmentSafety.corrupt, "Inhalt nicht dekodierbar"))
            continue
        if is_inline and content_type.startswith("image/") and not filename:
            # Logos/Signaturen: nicht speichern, aber protokollieren.
            attachments.append(ParsedAttachment(index, filename, content_type, detect_content_type(data), None,
                                                len(data), True, content_id, AttachmentSafety.inline_skipped))
            continue
        safety, detected, reason = classify_attachment(filename, data, max_attachment_bytes)
        attachments.append(ParsedAttachment(
            index, filename, content_type, detected, data if safety == AttachmentSafety.ok else None, len(data),
            is_inline, content_id, safety, reason,
        ))

    # Im HTML eingebettete Bilder (Signatur, Logo) haben oft einen Dateinamen. Erst nach dem
    # Durchlauf erkennbar, weil der HTML-Teil nicht immer vor den Bildern steht.
    for att in attachments:
        cid = (att.content_id or "").strip().strip("<>").lower()
        if (att.declared_content_type or "").startswith("image/") and cid and (
            cid in referenced_cids or (att.filename and _OUTLOOK_EMBEDDED.match(att.filename))
        ):
            att.safety, att.data, att.rejection_reason = AttachmentSafety.inline_skipped, None, None

    full_text = "\n".join(text_chunks)
    subject = _decode_header(msg, "Subject")
    return ParsedMessage(
        message_id=_decode_header(msg, "Message-ID"),
        subject=subject,
        from_address=(_addresses(msg, "From") or [None])[0],
        to_addresses=_addresses(msg, "To", "Cc", "Delivered-To"),
        header_date=header_date,
        text_body=full_text,
        attachments=attachments,
        invoice_links=_invoice_links(urls, full_text + " " + (subject or "")),
        body_mentions_invoice=bool(_INVOICE_WORDS.search(full_text + " " + (subject or ""))),
        parse_warnings=warnings,
        reply_references=re.findall(r"<[^<>\s]+>", " ".join(
            v for v in (_decode_header(msg, "In-Reply-To"), _decode_header(msg, "References")) if v))[:50],
    )
