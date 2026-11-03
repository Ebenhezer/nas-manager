"""Backup encryption (Phase 11): segmented AES-GCM streams, keys and blob names."""

from __future__ import annotations

import hashlib
import io
import struct

import pytest

from app.services.backup import crypto

SEGMENT = 64


@pytest.fixture(scope="module")
def keys() -> crypto.Keys:
    return crypto.derive_keys("correct horse battery staple", crypto.KdfParams.new(1024))


def _encrypt(data: bytes, keys: crypto.Keys) -> bytes:
    out = io.BytesIO()
    crypto.encrypt_stream(io.BytesIO(data), out, keys, segment_size=SEGMENT)
    return out.getvalue()


def _decrypt(blob: bytes, keys: crypto.Keys) -> bytes:
    out = io.BytesIO()
    crypto.decrypt_stream(io.BytesIO(blob), out, keys, segment_size=SEGMENT)
    return out.getvalue()


def _segments(blob: bytes) -> list[bytes]:
    """Split an encrypted stream into its framed segments (header excluded)."""
    body = blob[len(crypto.MAGIC) :]
    parts = []
    while body:
        (length,) = struct.unpack(">I", body[12:16])
        parts.append(body[: 16 + length])
        body = body[16 + length :]
    return parts


@pytest.mark.parametrize("size", [0, 1, SEGMENT - 1, SEGMENT, SEGMENT + 1, 5 * SEGMENT, 1000])
def test_round_trip(keys: crypto.Keys, size: int) -> None:
    data = bytes(i % 251 for i in range(size))
    out = io.BytesIO()
    sha256, length = crypto.encrypt_stream(io.BytesIO(data), out, keys, segment_size=SEGMENT)
    assert length == size
    assert _decrypt(out.getvalue(), keys) == data
    assert sha256 == hashlib.sha256(data).hexdigest()


def test_ciphertext_hides_plaintext_and_is_randomized(keys: crypto.Keys) -> None:
    data = b"top secret payroll " * 20
    first, second = _encrypt(data, keys), _encrypt(data, keys)
    assert b"payroll" not in first
    assert first != second  # random nonces


def test_wrong_key_is_rejected(keys: crypto.Keys) -> None:
    other = crypto.derive_keys("a different passphrase", crypto.KdfParams.new(1024))
    with pytest.raises(crypto.DecryptionError):
        _decrypt(_encrypt(b"hello world", keys), other)


def test_tampered_byte_is_rejected(keys: crypto.Keys) -> None:
    blob = bytearray(_encrypt(b"x" * 200, keys))
    blob[40] ^= 0x01
    with pytest.raises(crypto.DecryptionError):
        _decrypt(bytes(blob), keys)


def test_truncation_is_rejected(keys: crypto.Keys) -> None:
    blob = _encrypt(b"y" * (3 * SEGMENT + 5), keys)
    parts = _segments(blob)
    assert len(parts) == 4
    truncated = crypto.MAGIC + b"".join(parts[:-1])
    with pytest.raises(crypto.DecryptionError):
        _decrypt(truncated, keys)
    with pytest.raises(crypto.DecryptionError):
        _decrypt(blob[:-3], keys)


def test_reordering_and_trailing_data_are_rejected(keys: crypto.Keys) -> None:
    blob = _encrypt(b"z" * (3 * SEGMENT), keys)
    parts = _segments(blob)
    swapped = crypto.MAGIC + parts[1] + parts[0] + b"".join(parts[2:])
    with pytest.raises(crypto.DecryptionError):
        _decrypt(swapped, keys)
    with pytest.raises(crypto.DecryptionError):
        _decrypt(blob + parts[-1], keys)


def test_bad_header_is_rejected(keys: crypto.Keys) -> None:
    with pytest.raises(crypto.DecryptionError, match="header"):
        _decrypt(b"NOTBKP" + b"\x00" * 40, keys)


def test_seal_unseal(keys: crypto.Keys) -> None:
    assert crypto.unseal(crypto.seal(b"manifest", keys), keys) == b"manifest"


def test_blob_names_are_keyed(keys: crypto.Keys) -> None:
    other = crypto.derive_keys("correct horse battery staple", crypto.KdfParams.new(1024))
    digest = "ab" * 32
    assert keys.blob_name(digest) != digest
    assert keys.blob_name(digest) != other.blob_name(digest)  # different salt
    assert keys.blob_name(digest) == keys.blob_name(digest)


def test_same_passphrase_and_salt_derive_same_keys() -> None:
    params = crypto.KdfParams.new(1024)
    restored = crypto.KdfParams.from_dict(params.as_dict())
    assert crypto.derive_keys("pass phrase 1", params) == crypto.derive_keys(
        "pass phrase 1", restored
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"name": "pbkdf2"},
        {"name": "scrypt", "salt": "AAAA", "n": 1024, "r": 8, "p": 1},  # short salt
        {"name": "scrypt", "salt": "A" * 24, "n": 1000, "r": 8, "p": 1},  # not 2^k
        {"name": "scrypt", "salt": "A" * 24, "n": 2**22, "r": 8, "p": 1},  # too costly
        {"name": "scrypt", "salt": "A" * 24, "n": 1024, "r": 8, "p": 4},
        {"name": "scrypt", "salt": "!!", "n": 1024, "r": 8, "p": 1},
    ],
)
def test_hostile_kdf_parameters_are_refused(value: object) -> None:
    with pytest.raises(crypto.DecryptionError):
        crypto.KdfParams.from_dict(value)
