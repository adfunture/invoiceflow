"""Rechnungs-PDFs hinter Portal-Links automatisch laden – nur für freigegebene Domains.

Viele Anbieter (z. B. Stripe, Paddle) schicken statt eines Anhangs nur einen Link.
Für Domains aus der Einstellung ``links.auto_fetch_domains`` lädt der
Processing-Worker den Link selbst:

* Jeder Abruf und jede Weiterleitung läuft durch ``check_url`` (nur HTTPS,
  Port 443, ausschließlich öffentliche IP-Adressen – kein Zugriff auf interne Dienste).
* Der erste Link muss auf einer freigegebenen Domain liegen.
* Übernommen wird nur, was nach Inhalt (nicht nach Header) ein PDF oder eine
  XML-Rechnung ist – sonst bleibt die Mail in der Prüfung.
* Größe und Weiterleitungen sind begrenzt, es werden keine Cookies oder
  Zugangsdaten mitgeschickt.

Ist die Seite HTML (z. B. eine Rechnungsansicht), wird höchstens ein darin
verlinktes PDF auf einer freigegebenen Domain nachgeladen.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from invoiceflow.mail.mime import detect_content_type
from invoiceflow.mail.url_safety import UnsafeUrlError, check_url
from invoiceflow.services.app_settings import sender_matches

MAX_REDIRECTS = 5
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
USER_AGENT = "InvoiceFlow-Belegabruf/1.0"
_PDF_LINK = re.compile(r"""href\s*=\s*["']([^"'<>\s]+(?:\.pdf|/pdf)(?:\?[^"'<>\s]*)?)["']""", re.I)
# Stripe-Rechnungsansicht → direkter PDF-Download (Muster des Hosted-Invoice-Links)
_STRIPE_HOSTED = re.compile(r"^https://invoice\.stripe\.com/i/([^/?#]+)/([^/?#]+)")


class LinkFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class FetchedFile:
    url: str
    data: bytes
    content_type: str
    filename: str


Fetcher = Callable[[str, list[str], int], FetchedFile]


def allowed(url: str, domains: list[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return bool(host) and sender_matches(f"x@{host}", domains)


def _rewrite(url: str) -> str:
    if m := _STRIPE_HOSTED.match(url):
        return f"https://pay.stripe.com/invoice/{m.group(1)}/{m.group(2)}/pdf"
    return url


def _filename(url: str, kind: str) -> str:
    last = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or "rechnung"
    ext = "pdf" if kind == "application/pdf" else "xml"
    last = re.sub(r"[^A-Za-z0-9._-]", "_", last)[:80]
    return last if last.lower().endswith("." + ext) else f"{last}.{ext}"


def _get(client: httpx.Client, url: str, max_bytes: int) -> tuple[str, bytes, str]:
    """GET mit manuell geprüften Weiterleitungen. → (End-URL, Inhalt, Content-Type-Header)."""
    for _ in range(MAX_REDIRECTS + 1):
        try:
            check_url(url)
        except UnsafeUrlError as exc:
            raise LinkFetchError(f"Link nicht erlaubt: {exc}") from exc
        with client.stream("GET", url) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise LinkFetchError("Weiterleitung ohne Ziel")
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                raise LinkFetchError(f"HTTP {response.status_code}")
            declared = int(response.headers.get("content-length") or 0)
            if declared > max_bytes:
                raise LinkFetchError("Datei zu groß")
            data = bytearray()
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > max_bytes:
                    raise LinkFetchError("Datei zu groß")
            return url, bytes(data), response.headers.get("content-type", "")
    raise LinkFetchError("Zu viele Weiterleitungen")


def fetch_invoice(url: str, domains: list[str], max_bytes: int) -> FetchedFile:
    """Lädt die Rechnung hinter ``url``. Wirft ``LinkFetchError``, wenn nichts Verwertbares kommt."""
    url = _rewrite(url)
    if not allowed(url, domains):
        raise LinkFetchError("Domain nicht freigegeben")
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False, headers={"User-Agent": USER_AGENT}) as client:
        final, data, header_type = _get(client, url, max_bytes)
        kind = detect_content_type(data)
        if kind in ("application/pdf", "application/xml"):
            return FetchedFile(final, data, kind, _filename(final, kind))
        if "html" in header_type.lower():
            # Rechnungsansicht: genau ein Nachladen eines verlinkten PDFs auf freigegebener Domain.
            for href in _PDF_LINK.findall(data[:500_000].decode("utf-8", "replace")):
                target = urljoin(final, href.replace("&amp;", "&"))
                if allowed(target, domains):
                    final2, data2, _ = _get(client, target, max_bytes)
                    kind2 = detect_content_type(data2)
                    if kind2 in ("application/pdf", "application/xml"):
                        return FetchedFile(final2, data2, kind2, _filename(final2, kind2))
                    break
    raise LinkFetchError("Link liefert kein PDF/keine XML-Rechnung")
