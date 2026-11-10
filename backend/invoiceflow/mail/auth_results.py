"""Absender-Echtheit aus ``Authentication-Results`` (RFC 8601).

Der empfangende Mailserver (ALL-INKL, Strato …) prüft SPF, DKIM und DMARC und
schreibt das Ergebnis als obersten ``Authentication-Results``-Header in die Mail.
Weiter unten stehende Header kann ein Absender selbst mitschicken – deshalb zählt
nur der oberste.

Ergebnis:
* ``pass`` – DMARC bestanden, oder DKIM/SPF bestanden *und* zur From-Domain passend
* ``fail`` – Header vorhanden, aber nichts davon bestanden
* ``none`` – kein Header (Server prüft nicht oder Upload)

Genutzt wird das nur für *positive* Absenderregeln (Beleg gilt als Rechnung).
Für Blocklisten ist eine Fälschung harmlos und wird nicht geprüft.
"""
from __future__ import annotations

import re
from email.message import Message

_METHOD = re.compile(r"\b(dmarc|dkim|spf)\s*=\s*([a-z]+)([^;]*)", re.I)
_PROP = re.compile(r"\b(header\.from|header\.d|header\.i|smtp\.mailfrom)\s*=\s*\"?([^\s;\"]+)", re.I)


def _domain(value: str | None) -> str:
    if not value:
        return ""
    return value.rsplit("@", 1)[-1].strip().strip(">").lower()


def _aligned(domain: str, from_domain: str) -> bool:
    # Relaxed Alignment: gleiche Domain oder Subdomain (mail.paypal.de passt zu paypal.de und umgekehrt).
    return bool(domain and from_domain) and (
        domain == from_domain or domain.endswith("." + from_domain) or from_domain.endswith("." + domain))


def evaluate(header_value: str | None, from_address: str | None) -> str:
    if not header_value:
        return "none"
    from_domain = _domain(from_address)
    for method, result, rest in _METHOD.findall(header_value):
        if result.lower() != "pass":
            continue
        method = method.lower()
        props = {k.lower(): v for k, v in _PROP.findall(rest)}
        if method == "dmarc":
            # header.from fehlt bei manchen Servern; dann gilt DMARC für die From-Domain der Mail.
            if not props.get("header.from") or _aligned(_domain(props["header.from"]), from_domain):
                return "pass"
        elif method == "dkim" and _aligned(_domain(props.get("header.d") or props.get("header.i")), from_domain):
            return "pass"
        elif method == "spf" and _aligned(_domain(props.get("smtp.mailfrom")), from_domain):
            return "pass"
    return "fail"


def sender_auth(msg: Message, from_address: str | None) -> str:
    try:
        headers = msg.get_all("Authentication-Results", []) or []
    except Exception:  # noqa: BLE001 – kaputter Header zählt wie keiner
        return "none"
    return evaluate(str(headers[0]) if headers else None, from_address)
