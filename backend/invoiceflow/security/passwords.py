"""Passwort-Hashing (Argon2id) und Mindestanforderungen."""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # Argon2id, RFC-9106-Standardparameter
# Für unbekannte Konten wird gegen diesen Hash geprüft, damit die Antwortzeit
# nicht verrät, ob ein Konto existiert.
_DUMMY_HASH = _hasher.hash("invoiceflow-timing-equalizer")

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    validate_password_strength(password)
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen lang sein")
    classes = sum(
        [
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        ]
    )
    if classes < 3:
        raise ValueError("Passwort braucht mindestens drei von: Kleinbuchstaben, Großbuchstaben, Ziffern, Sonderzeichen")
