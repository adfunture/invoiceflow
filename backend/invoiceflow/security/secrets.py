"""Auflösung von Zugangsdaten-Referenzen.

In PostgreSQL steht nur der *Name* einer Umgebungsvariable (z. B.
``INVOICEFLOW_SECRET_IMAP_ADFUNTURE_INFO``). Der Wert wird in Railway als
Variable gepflegt und erst im Moment des Verbindungsaufbaus gelesen.
"""
from __future__ import annotations

import os
import re

from invoiceflow.config import SECRET_ENV_PREFIX

CREDENTIAL_REF_PATTERN = re.compile(rf"^{SECRET_ENV_PREFIX}[A-Z0-9_]+$")


class MissingSecretError(RuntimeError):
    """Die referenzierte Variable ist nicht gesetzt (Konfigurationsschritt offen)."""


def validate_credential_ref(ref: str) -> str:
    if not CREDENTIAL_REF_PATTERN.fullmatch(ref):
        raise ValueError(f"Zugangsdaten-Referenz muss dem Muster {SECRET_ENV_PREFIX}<NAME> entsprechen (A-Z, 0-9, _)")
    return ref


def is_secret_configured(ref: str) -> bool:
    return bool(CREDENTIAL_REF_PATTERN.fullmatch(ref)) and bool(os.environ.get(ref))


def resolve_secret(ref: str) -> str:
    validate_credential_ref(ref)
    value = os.environ.get(ref)
    if not value:
        raise MissingSecretError(f"Railway-Variable {ref} ist nicht gesetzt")
    return value
