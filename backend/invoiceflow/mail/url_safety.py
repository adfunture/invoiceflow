"""SSRF-Schutz für Rechnungslinks aus E-Mails.

InvoiceFlow ruft Links aus E-Mails nie automatisch auf. Erst wenn ein
Prüfer im Dashboard einen Link ausdrücklich freigibt (Phase 5), wird er über
diese Prüfung geladen: nur HTTPS, Standardport, keine Zugangsdaten in der
URL, und der Hostname muss ausschließlich auf öffentliche IP-Adressen zeigen.
Die geprüften IPs werden anschließend für die Verbindung verwendet
(kein DNS-Rebinding zwischen Prüfung und Abruf).
"""
from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

Resolver = Callable[[str], list[str]]


class UnsafeUrlError(ValueError):
    pass


@dataclass(frozen=True)
class CheckedUrl:
    url: str
    host: str
    port: int
    addresses: list[str]


def _system_resolver(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})


def is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local)


def check_url(url: str, resolver: Resolver = _system_resolver) -> CheckedUrl:
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        raise UnsafeUrlError("Nur https-Links werden geladen")
    if parts.username or parts.password:
        raise UnsafeUrlError("Zugangsdaten in der URL sind nicht erlaubt")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise UnsafeUrlError("Kein Host")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise UnsafeUrlError("Ungültiger Port") from exc
    if port != 443:
        raise UnsafeUrlError("Nur Port 443")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".railway.internal")):
        raise UnsafeUrlError("Interner Hostname")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    addresses = [str(literal)] if literal else resolver(host)
    if not addresses:
        raise UnsafeUrlError("Host nicht auflösbar")
    bad = [a for a in addresses if not is_public_ip(a)]
    if bad:
        raise UnsafeUrlError(f"Host zeigt auf nicht-öffentliche Adresse ({', '.join(bad)})")
    return CheckedUrl(url=url, host=host, port=port, addresses=addresses)
