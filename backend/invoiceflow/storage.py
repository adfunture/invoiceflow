"""Verschlüsselter, inhaltsadressierter Dokumentenspeicher.

* Jede Datei wird mit AES-256-GCM verschlüsselt. Der Speicher-Key ist
  Associated Data: Ein Blob, der unter einen anderen Key kopiert wird, lässt
  sich nicht mehr entschlüsseln.
* Keys werden aus Unternehmen, Art und SHA-256 des Klartexts gebildet. Ein
  vorhandenes Objekt wird nie überschrieben – Originale bleiben unverändert.
* Backends: lokales Dateisystem (Entwicklung/Tests) und S3-kompatibel
  (Railway Bucket o. ä.) für den Betrieb.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from invoiceflow.config import Settings, StorageBackend, get_settings

_MAGIC = b"IFE1"  # Format-Version 1: MAGIC | nonce(12) | ciphertext+tag
_NONCE_LEN = 12
_KEY_PATTERN = re.compile(r"^[a-z0-9_-]+/[a-z_]+/[0-9a-f]{2}/[0-9a-f]{64}\.[a-z0-9]+\.enc$")
KINDS = {"raw_email", "attachment", "embedded_xml"}


class StorageError(RuntimeError):
    pass


class IntegrityError(StorageError):
    """Entschlüsselung/Prüfung fehlgeschlagen – Datei manipuliert oder falscher Schlüssel."""


def generate_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()


def _load_key(encoded: str | None) -> bytes:
    if not encoded:
        raise StorageError("DOCUMENT_ENCRYPTION_KEY ist nicht gesetzt (invoiceflow generate-key)")
    try:
        key = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise StorageError("DOCUMENT_ENCRYPTION_KEY ist kein gültiges base64") from exc
    if len(key) != 32:
        raise StorageError("DOCUMENT_ENCRYPTION_KEY muss 32 Byte lang sein")
    return key


class BlobBackend(Protocol):
    def exists(self, key: str) -> bool: ...
    def write_new(self, key: str, data: bytes) -> bool:
        """Schreibt nur, wenn der Key noch nicht existiert. True = neu geschrieben."""
    def read(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class LocalBackend:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise StorageError("Ungültiger Speicher-Key")
        if os.name == "nt" and len(str(path)) > 200 and not str(path).startswith("\\\\?\\"):
            # Windows-Pfadlimit (260 Zeichen) umgehen; nur für lokale Entwicklung relevant.
            return Path("\\\\?\\" + str(path))
        return path

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def write_new(self, key: str, data: bytes) -> bool:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        tmp.write_bytes(data)
        try:
            os.link(tmp, path)  # atomar und schlägt fehl, wenn das Ziel existiert
            return True
        except FileExistsError:
            return False
        finally:
            tmp.unlink(missing_ok=True)

    def read(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as exc:
            raise StorageError(f"Objekt fehlt: {key}") from exc

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3Backend:
    def __init__(self, settings: Settings) -> None:
        import boto3
        from botocore.config import Config

        if not settings.s3_bucket:
            raise StorageError("S3_BUCKET ist nicht gesetzt")
        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            config=Config(retries={"max_attempts": 5, "mode": "standard"}, signature_version="s3v4"),
        )

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def write_new(self, key: str, data: bytes) -> bool:
        from botocore.exceptions import ClientError

        if self.exists(key):
            return False
        try:
            # IfNoneMatch: bedingtes Schreiben, falls der Anbieter es unterstützt (S3 seit 2024).
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, IfNoneMatch="*",
                                   ContentType="application/octet-stream")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"PreconditionFailed", "412"}:
                return False
            raise
        return True

    def read(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


@dataclass(frozen=True)
class StoredObject:
    key: str
    sha256: str
    size_bytes: int
    newly_written: bool


class DocumentStore:
    def __init__(self, backend: BlobBackend, encryption_key: bytes) -> None:
        self.backend = backend
        self._aead = AESGCM(encryption_key)

    @staticmethod
    def make_key(company_slug: str, kind: str, sha256: str, extension: str) -> str:
        if kind not in KINDS:
            raise StorageError(f"Unbekannte Art: {kind}")
        ext = re.sub(r"[^a-z0-9]", "", extension.lower())[:10] or "bin"
        key = f"{company_slug}/{kind}/{sha256[:2]}/{sha256}.{ext}.enc"
        if not _KEY_PATTERN.fullmatch(key):
            raise StorageError("Ungültiger Speicher-Key")
        return key

    def put(self, company_slug: str, kind: str, data: bytes, extension: str = "bin") -> StoredObject:
        sha = hashlib.sha256(data).hexdigest()
        key = self.make_key(company_slug, kind, sha, extension)
        if self.backend.exists(key):
            return StoredObject(key, sha, len(data), newly_written=False)
        nonce = secrets.token_bytes(_NONCE_LEN)
        blob = _MAGIC + nonce + self._aead.encrypt(nonce, data, key.encode())
        written = self.backend.write_new(key, blob)
        return StoredObject(key, sha, len(data), newly_written=written)

    def get(self, key: str, expected_sha256: str | None = None) -> bytes:
        blob = self.backend.read(key)
        if not blob.startswith(_MAGIC) or len(blob) < len(_MAGIC) + _NONCE_LEN + 16:
            raise IntegrityError(f"Unbekanntes Format: {key}")
        nonce = blob[len(_MAGIC):len(_MAGIC) + _NONCE_LEN]
        try:
            data = self._aead.decrypt(nonce, blob[len(_MAGIC) + _NONCE_LEN:], key.encode())
        except InvalidTag as exc:
            raise IntegrityError(f"Entschlüsselung fehlgeschlagen: {key}") from exc
        if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256:
            raise IntegrityError(f"Hash stimmt nicht: {key}")
        return data


    def delete(self, key: str) -> None:
        """Nur für die Aufbewahrungslogik (ops.retention); prüft dort vorher alle Verweise."""
        if not _KEY_PATTERN.fullmatch(key):
            raise StorageError("Ungültiger Speicher-Key")
        self.backend.delete(key)


def build_store(settings: Settings) -> DocumentStore:
    key = _load_key(settings.document_encryption_key)
    if settings.storage_backend == StorageBackend.s3:
        return DocumentStore(S3Backend(settings), key)
    return DocumentStore(LocalBackend(settings.storage_local_path), key)


@lru_cache
def get_store() -> DocumentStore:
    return build_store(get_settings())
