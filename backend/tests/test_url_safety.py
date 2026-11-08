from __future__ import annotations

import pytest

from invoiceflow.mail.url_safety import UnsafeUrlError, check_url


def resolver(mapping):
    return lambda host: mapping.get(host, [])


PUBLIC = resolver({"portal.example.com": ["93.184.216.34"], "rebind.example.com": ["93.184.216.34", "10.0.0.5"],
                   "meta.example.com": ["169.254.169.254"], "v6local.example.com": ["::1"]})


def test_public_https_allowed():
    checked = check_url("https://portal.example.com/invoice/1.pdf", PUBLIC)
    assert checked.addresses == ["93.184.216.34"]


@pytest.mark.parametrize("url", [
    "http://portal.example.com/x",              # kein TLS
    "https://user:pw@portal.example.com/x",     # Zugangsdaten
    "https://portal.example.com:8443/x",        # anderer Port
    "https://localhost/x",
    "https://127.0.0.1/x",
    "https://10.1.2.3/x",
    "https://[::1]/x",
    "https://meta.example.com/latest",          # Cloud-Metadaten
    "https://rebind.example.com/x",             # eine private Adresse genügt
    "https://v6local.example.com/x",
    "https://postgres.railway.internal/x",
    "file:///etc/passwd",
    "https://unbekannt.example.com/x",          # nicht auflösbar
])
def test_unsafe_urls_rejected(url):
    with pytest.raises(UnsafeUrlError):
        check_url(url, PUBLIC)
