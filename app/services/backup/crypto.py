"""Backup encryption (Phase 11): scrypt, HKDF and segmented AES-256-GCM.

A repository passphrase is stretched with scrypt (parameters and salt are
stored in the repository's ``repo.json``) into a master key. HKDF-SHA256 then
derives two independent subkeys:

* the *content* key encrypts every blob, snapshot manifest and sealed
  configuration export with AES-256-GCM, and
* the *name* key turns a file's SHA-256 into its blob name with HMAC-SHA256,
  so the storage target never learns the content hashes (which would reveal
  whether a known file is stored).

Content is framed into segments so arbitrarily large files stream in
constant memory. Every segment has its own random 96-bit nonce, and its
index and a "final segment" flag are authenticated as associated data, so
segments cannot be reordered, dropped, duplicated or truncated without the
decryption failing.

Stream layout::

    MAGIC (6 bytes)
    repeated: nonce (12) | ciphertext length (4, big endian) | ciphertext+tag
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import struct
from dataclasses import dataclass
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"NASBK1"
SEGMENT_SIZE = 4 * 1024 * 1024
_NONCE_BYTES = 12
_TAG_BYTES = 16
_LENGTH = struct.Struct(">I")
_AAD = struct.Struct(">QB")
SCRYPT_R = 8
SCRYPT_P = 1
MIN_PASSPHRASE_LENGTH = 12


class DecryptionError(Exception):
    """Wrong key, or the data was corrupted or tampered with."""


@dataclass(frozen=True)
class KdfParams:
    salt: bytes
    n: int
    r: int = SCRYPT_R
    p: int = SCRYPT_P

    @classmethod
    def new(cls, n: int) -> KdfParams:
        return cls(salt=os.urandom(16), n=n)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": "scrypt",
            "salt": base64.b64encode(self.salt).decode(),
            "n": self.n,
            "r": self.r,
            "p": self.p,
        }

    @classmethod
    def from_dict(cls, value: object) -> KdfParams:
        if not isinstance(value, dict) or value.get("name") != "scrypt":
            raise DecryptionError("Unsupported key derivation parameters")
        try:
            salt = base64.b64decode(str(value["salt"]), validate=True)
            n, r, p = int(value["n"]), int(value["r"]), int(value["p"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DecryptionError("Malformed key derivation parameters") from exc
        # Bound the work a hostile repo.json can make us do.
        if len(salt) < 16 or n < 2 or n & (n - 1) or n > 2**20 or not 1 <= r <= 32 or p != 1:
            raise DecryptionError("Key derivation parameters out of range")
        return cls(salt=salt, n=n, r=r, p=p)


@dataclass(frozen=True)
class Keys:
    content: bytes
    names: bytes

    def blob_name(self, sha256_hex: str) -> str:
        return hmac.new(self.names, bytes.fromhex(sha256_hex), hashlib.sha256).hexdigest()


def derive_keys(passphrase: str, params: KdfParams) -> Keys:
    master = Scrypt(salt=params.salt, length=32, n=params.n, r=params.r, p=params.p).derive(
        passphrase.encode("utf-8")
    )
    return Keys(
        content=_hkdf(master, b"nas-backup content"), names=_hkdf(master, b"nas-backup names")
    )


def random_keys() -> Keys:
    """Fresh keys without a passphrase (used to seal short-lived data)."""
    return Keys(content=os.urandom(32), names=os.urandom(32))


def _hkdf(master: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(master)


def _aad(index: int, final: bool) -> bytes:
    return MAGIC + _AAD.pack(index, 1 if final else 0)


def encrypt_stream(
    source: BinaryIO, out: BinaryIO, keys: Keys, *, segment_size: int = SEGMENT_SIZE
) -> tuple[str, int]:
    """Encrypt ``source`` into ``out``; return the plaintext SHA-256 and size."""
    aead = AESGCM(keys.content)
    digest = hashlib.sha256()
    size = 0
    out.write(MAGIC)
    index = 0
    current = source.read(segment_size)
    while True:
        following = source.read(segment_size) if current else b""
        final = not following
        digest.update(current)
        size += len(current)
        nonce = os.urandom(_NONCE_BYTES)
        sealed = aead.encrypt(nonce, current, _aad(index, final))
        out.write(nonce + _LENGTH.pack(len(sealed)) + sealed)
        if final:
            break
        current = following
        index += 1
    return digest.hexdigest(), size


def decrypt_stream(
    source: BinaryIO, out: BinaryIO, keys: Keys, *, segment_size: int = SEGMENT_SIZE
) -> tuple[str, int]:
    """Decrypt ``source`` into ``out``; return the plaintext SHA-256 and size.

    Raises ``DecryptionError`` on a wrong key, corruption, truncation,
    reordering or trailing data. ``out`` may hold partial plaintext when it
    raises, so callers write into a temporary file.
    """
    aead = AESGCM(keys.content)
    if source.read(len(MAGIC)) != MAGIC:
        raise DecryptionError("Not a backup object (bad header)")
    digest = hashlib.sha256()
    size = 0
    index = 0
    header_size = _NONCE_BYTES + _LENGTH.size
    limit = max(segment_size, SEGMENT_SIZE) + _TAG_BYTES
    header = source.read(header_size)
    while True:
        if len(header) != header_size:
            raise DecryptionError("Backup object is truncated")
        nonce = header[:_NONCE_BYTES]
        (length,) = _LENGTH.unpack(header[_NONCE_BYTES:])
        if not _TAG_BYTES <= length <= limit:
            raise DecryptionError("Backup object is corrupted (segment length)")
        sealed = source.read(length)
        if len(sealed) != length:
            raise DecryptionError("Backup object is truncated")
        header = source.read(header_size)
        # The segment claims to be the last one exactly when nothing follows.
        # A truncated stream therefore fails: its new last segment was
        # sealed as non-final.
        final = not header
        try:
            plain = aead.decrypt(nonce, sealed, _aad(index, final))
        except InvalidTag as exc:
            raise DecryptionError(
                "Backup object failed authentication (wrong key, corrupted or truncated)"
            ) from exc
        digest.update(plain)
        size += len(plain)
        out.write(plain)
        if final:
            return digest.hexdigest(), size
        index += 1


def seal(data: bytes, keys: Keys) -> bytes:
    """Encrypt a small in-memory value (snapshot manifests, exports)."""
    out = io.BytesIO()
    encrypt_stream(io.BytesIO(data), out, keys)
    return out.getvalue()


def unseal(data: bytes, keys: Keys) -> bytes:
    out = io.BytesIO()
    decrypt_stream(io.BytesIO(data), out, keys)
    return out.getvalue()
