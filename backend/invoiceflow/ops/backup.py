"""Verschlüsselte Datenbank-Backups und Prüfungen.

* ``pg_dump --format=custom`` → AES-256-GCM in Blöcken → eigener Backup-Bucket
  (getrennt vom Dokumentenspeicher; am besten anderer Anbieter/Account).
* Zugangsdaten gehen über Umgebungsvariablen an pg_dump, nicht über die Kommandozeile.
* ``restore_check`` lädt ein Backup, entschlüsselt es und lässt ``pg_restore --list``
  das Archiv lesen – Nachweis, dass das Backup wiederherstellbar ist.
* ``verify_storage`` entschlüsselt gespeicherte Originale und prüft ihren SHA-256.

Die Railway-Postgres-Backups laufen zusätzlich; dieses Backup schützt auch gegen
Verlust des Railway-Projekts.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import struct
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from invoiceflow.config import Settings
from invoiceflow.db.models import Document, EmailAttachment
from invoiceflow.storage import DocumentStore, StorageError, _load_key

MAGIC = b"IFB1"
CHUNK = 8 * 1024 * 1024
REQUIRED_TABLES = ("companies", "mailboxes", "email_messages", "documents", "invoice_metadata", "datev_transmissions", "audit_logs")

Runner = Callable[..., subprocess.CompletedProcess]


class BackupError(RuntimeError):
    pass


# --- Verschlüsselung in Blöcken -------------------------------------------------


def encrypt_file(src: Path, dst: Path, key: bytes, label: str) -> str:
    """Format: MAGIC | wiederholt [nonce(12) | länge(4) | ciphertext]. AAD bindet Label, Blocknummer und Ende-Flag."""
    aead = AESGCM(key)
    digest = hashlib.sha256()
    with src.open("rb") as fin, dst.open("wb") as fout:
        fout.write(MAGIC)
        index = 0
        chunk = fin.read(CHUNK)
        while True:
            following = fin.read(CHUNK)
            last = not following
            digest.update(chunk)
            nonce = secrets.token_bytes(12)
            aad = f"{label}|{index}|{int(last)}".encode()
            ciphertext = aead.encrypt(nonce, chunk, aad)
            fout.write(nonce + struct.pack(">I", len(ciphertext)) + ciphertext)
            if last:
                break
            chunk, index = following, index + 1
    return digest.hexdigest()


def decrypt_file(src: Path, dst: Path, key: bytes, label: str) -> str:
    aead = AESGCM(key)
    digest = hashlib.sha256()
    with src.open("rb") as fin, dst.open("wb") as fout:
        if fin.read(4) != MAGIC:
            raise BackupError("Kein InvoiceFlow-Backup")
        index, last_seen = 0, False
        while header := fin.read(16):
            if last_seen:
                raise BackupError("Daten nach dem letzten Block – Backup manipuliert")
            nonce, length = header[:12], struct.unpack(">I", header[12:])[0]
            ciphertext = fin.read(length)
            for last in (False, True):
                try:
                    plain = aead.decrypt(nonce, ciphertext, f"{label}|{index}|{int(last)}".encode())
                    last_seen = last
                    break
                except InvalidTag:
                    continue
            else:
                raise BackupError(f"Block {index} nicht entschlüsselbar – falscher Schlüssel oder manipuliert")
            digest.update(plain)
            fout.write(plain)
            index += 1
        if not last_seen:
            raise BackupError("Backup unvollständig (letzter Block fehlt)")
    return digest.hexdigest()


# --- Ziele ------------------------------------------------------------------------


class BackupTarget(Protocol):
    def upload(self, key: str, path: Path) -> None: ...
    def download(self, key: str, path: Path) -> None: ...
    def list(self, prefix: str) -> list[tuple[str, datetime]]: ...
    def delete(self, key: str) -> None: ...


class LocalTarget:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    def _p(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise BackupError("Ungültiger Backup-Key")
        return p

    def upload(self, key: str, path: Path) -> None:
        dst = self._p(key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(path.read_bytes())

    def download(self, key: str, path: Path) -> None:
        path.write_bytes(self._p(key).read_bytes())

    def list(self, prefix: str) -> list[tuple[str, datetime]]:
        base = self.root / prefix
        if not base.exists():
            return []
        return sorted(
            (str(p.relative_to(self.root)).replace("\\", "/"), datetime.fromtimestamp(p.stat().st_mtime, UTC))
            for p in base.rglob("*.enc")
        )

    def delete(self, key: str) -> None:
        self._p(key).unlink(missing_ok=True)


class S3Target:
    def __init__(self, settings: Settings) -> None:
        import boto3

        self.bucket = settings.backup_s3_bucket
        self.client = boto3.client(
            "s3", endpoint_url=settings.backup_s3_endpoint_url, region_name=settings.backup_s3_region,
            aws_access_key_id=settings.backup_s3_access_key_id, aws_secret_access_key=settings.backup_s3_secret_access_key,
        )

    def upload(self, key: str, path: Path) -> None:
        self.client.upload_file(str(path), self.bucket, key)

    def download(self, key: str, path: Path) -> None:
        self.client.download_file(self.bucket, key, str(path))

    def list(self, prefix: str) -> list[tuple[str, datetime]]:
        out: list[tuple[str, datetime]] = []
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            out += [(o["Key"], o["LastModified"]) for o in page.get("Contents", [])]
        return sorted(out)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def get_target(settings: Settings) -> BackupTarget:
    if settings.backup_s3_bucket:
        return S3Target(settings)
    if settings.backup_local_path and not settings.is_production:
        return LocalTarget(settings.backup_local_path)
    raise BackupError("Kein Backup-Ziel konfiguriert (BACKUP_S3_BUCKET)")


def backup_key(settings: Settings) -> bytes:
    return _load_key(settings.backup_encryption_key or settings.document_encryption_key)


# --- Datenbank ----------------------------------------------------------------------


def _pg_env(settings: Settings) -> dict[str, str]:
    url = make_url(settings.database_url)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("INVOICEFLOW_SECRET_", "PG"))}
    env.update({
        "PGHOST": url.host or "localhost", "PGPORT": str(url.port or 5432), "PGUSER": url.username or "",
        "PGPASSWORD": url.password or "", "PGDATABASE": url.database or "",
    })
    if sslmode := url.query.get("sslmode"):
        env["PGSSLMODE"] = sslmode if isinstance(sslmode, str) else sslmode[0]
    return env


@dataclass
class BackupResult:
    key: str
    size_bytes: int
    sha256: str
    deleted_old: list[str]


def run_backup(settings: Settings, target: BackupTarget, *, runner: Runner = subprocess.run,
               now: datetime | None = None) -> BackupResult:
    now = now or datetime.now(UTC)
    key = backup_key(settings)
    object_key = f"db/{now:%Y/%m}/invoiceflow-{now:%Y%m%dT%H%M%SZ}.dump.enc"
    with tempfile.TemporaryDirectory(prefix="invoiceflow-backup-") as tmp:
        dump, enc = Path(tmp) / "db.dump", Path(tmp) / "db.dump.enc"
        proc = runner(["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--file", str(dump)],
                      env=_pg_env(settings), capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            raise BackupError(f"pg_dump fehlgeschlagen: {(proc.stderr or '')[-500:]}")
        if not dump.exists() or dump.stat().st_size == 0:
            raise BackupError("pg_dump hat keine Daten geschrieben")
        sha = encrypt_file(dump, enc, key, object_key)
        target.upload(object_key, enc)
        size = enc.stat().st_size
    deleted = []
    cutoff = now - timedelta(days=settings.backup_keep_days)
    existing = target.list("db/")
    for k, modified in existing:
        if modified < cutoff and k != object_key and len(existing) - len(deleted) > 7:  # immer mind. 7 behalten
            target.delete(k)
            deleted.append(k)
    return BackupResult(object_key, size, sha, deleted)


@dataclass
class RestoreCheck:
    key: str
    sha256: str
    entries: int
    tables: list[str]
    missing_tables: list[str]


def restore_check(settings: Settings, target: BackupTarget, object_key: str | None = None, *,
                  runner: Runner = subprocess.run) -> RestoreCheck:
    if object_key is None:
        existing = target.list("db/")
        if not existing:
            raise BackupError("Kein Backup vorhanden")
        object_key = existing[-1][0]
    with tempfile.TemporaryDirectory(prefix="invoiceflow-restore-") as tmp:
        enc, dump = Path(tmp) / "db.dump.enc", Path(tmp) / "db.dump"
        target.download(object_key, enc)
        sha = decrypt_file(enc, dump, backup_key(settings), object_key)
        proc = runner(["pg_restore", "--list", str(dump)], capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise BackupError(f"pg_restore kann das Archiv nicht lesen: {(proc.stderr or '')[-500:]}")
    lines = [ln for ln in proc.stdout.splitlines() if ln and not ln.startswith(";")]
    tables: set[str] = set()
    for ln in lines:  # z. B. "215; 0 16390 TABLE DATA public companies postgres"
        parts = ln.split()
        if " TABLE DATA " in ln and (i := parts.index("DATA")) + 2 < len(parts):
            tables.add(parts[i + 2])
    tables = sorted(tables)
    return RestoreCheck(object_key, sha, len(lines), tables, [t for t in REQUIRED_TABLES if t not in tables])


# --- Dokumentenspeicher -----------------------------------------------------------


@dataclass
class StorageCheck:
    checked: int
    failed: list[str]


def verify_storage(db: Session, store: DocumentStore, sample: int | None = None) -> StorageCheck:
    """Entschlüsselt Originale und prüft den Hash. ``sample``: zufällige Auswahl statt aller."""
    order = func.random() if sample else Document.id
    docs = db.execute(select(Document.storage_key, Document.sha256).order_by(order).limit(sample)).all()
    atts = db.execute(select(EmailAttachment.storage_key, EmailAttachment.sha256)
                      .where(EmailAttachment.storage_key.is_not(None)).order_by(func.random() if sample else EmailAttachment.id)
                      .limit(sample)).all()
    failed: list[str] = []
    seen: set[str] = set()
    for key, sha in [*docs, *atts]:
        if key in seen:
            continue
        seen.add(key)
        try:
            store.get(key, sha)
        except (StorageError, OSError) as exc:
            failed.append(f"{key}: {type(exc).__name__}")
    return StorageCheck(len(seen), failed)
