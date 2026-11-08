from __future__ import annotations

import hashlib

import pytest

from invoiceflow.storage import DocumentStore, IntegrityError, LocalBackend, StorageError, generate_key, _load_key

KEY = b"s" * 32


@pytest.fixture
def store(tmp_path) -> DocumentStore:
    return DocumentStore(LocalBackend(tmp_path), KEY)


def test_roundtrip_and_content_addressing(store):
    data = b"%PDF-1.7 Rechnung"
    obj = store.put("adfunture", "attachment", data, "pdf")
    assert obj.sha256 == hashlib.sha256(data).hexdigest()
    assert obj.key == f"adfunture/attachment/{obj.sha256[:2]}/{obj.sha256}.pdf.enc"
    assert obj.newly_written
    assert store.get(obj.key, obj.sha256) == data


def test_original_is_never_overwritten(store):
    first = store.put("adfunture", "attachment", b"original", "pdf")
    before = store.backend.read(first.key)
    second = store.put("adfunture", "attachment", b"original", "pdf")
    assert second.key == first.key and second.newly_written is False
    assert store.backend.read(first.key) == before
    assert store.backend.write_new(first.key, b"anderer Inhalt") is False
    assert store.backend.read(first.key) == before


def test_companies_have_separate_prefixes(store):
    a = store.put("adfunture", "attachment", b"gleich", "pdf")
    b = store.put("sbc", "attachment", b"gleich", "pdf")
    assert a.key != b.key and a.key.startswith("adfunture/") and b.key.startswith("sbc/")


def test_ciphertext_does_not_contain_plaintext(store):
    obj = store.put("sbc", "raw_email", b"Subject: Rechnung 4711", "eml")
    assert b"Rechnung 4711" not in store.backend.read(obj.key)


def test_tampering_is_detected(store, tmp_path):
    obj = store.put("sbc", "attachment", b"daten", "pdf")
    path = tmp_path / obj.key
    blob = bytearray(path.read_bytes())
    blob[-1] ^= 0x01
    path.write_bytes(bytes(blob))
    with pytest.raises(IntegrityError):
        store.get(obj.key)


def test_blob_bound_to_its_key(store, tmp_path):
    """Ein Blob unter einen fremden Key kopiert lässt sich nicht entschlüsseln."""
    a = store.put("sbc", "attachment", b"rechnung sbc", "pdf")
    b = store.put("adfunture", "attachment", b"rechnung adfunture", "pdf")
    (tmp_path / b.key).write_bytes((tmp_path / a.key).read_bytes())
    with pytest.raises(IntegrityError):
        store.get(b.key)


def test_wrong_key_cannot_decrypt(store, tmp_path):
    obj = store.put("sbc", "attachment", b"x", "pdf")
    other = DocumentStore(LocalBackend(tmp_path), b"o" * 32)
    with pytest.raises(IntegrityError):
        other.get(obj.key)


def test_path_traversal_rejected(store):
    with pytest.raises(StorageError):
        store.backend.read("../../etc/passwd")
    with pytest.raises(StorageError):
        store.make_key("../x", "attachment", "a" * 64, "pdf")
    with pytest.raises(StorageError):
        store.make_key("sbc", "beliebig", "a" * 64, "pdf")


def test_key_validation():
    assert len(_load_key(generate_key())) == 32
    with pytest.raises(StorageError):
        _load_key(None)
    with pytest.raises(StorageError):
        _load_key("zu-kurz")


def test_long_local_paths_work(tmp_path):
    deep = tmp_path / ("x" * 120) / ("y" * 60)
    store = DocumentStore(LocalBackend(deep), KEY)
    obj = store.put("adfunture", "raw_email", b"lange pfade", "eml")
    assert store.get(obj.key, obj.sha256) == b"lange pfade"
    assert store.put("adfunture", "raw_email", b"lange pfade", "eml").newly_written is False
