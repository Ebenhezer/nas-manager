"""Loopback syslog collectors for the Phase 13 tests (UDP, TCP and TLS)."""

from __future__ import annotations

import datetime as dt
import ipaddress
import socket
import ssl
import threading
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def parse_octet_frames(data: bytes) -> list[bytes]:
    """Split an RFC 6587 octet-counted stream into messages."""
    messages: list[bytes] = []
    while data:
        length, _, rest = data.partition(b" ")
        size = int(length)
        messages.append(rest[:size])
        data = rest[size:]
    return messages


class Receiver:
    """Collects what clients send. ``messages`` holds whole datagrams (UDP)
    or octet-counted frames (TCP/TLS)."""

    def __init__(self, protocol: str, context: ssl.SSLContext | None = None) -> None:
        self.protocol = protocol
        self.context = context
        self.raw = bytearray()
        self.datagrams: list[bytes] = []
        self._lock = threading.Lock()
        kind = socket.SOCK_DGRAM if protocol == "udp" else socket.SOCK_STREAM
        self.sock = socket.socket(socket.AF_INET, kind)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port: int = self.sock.getsockname()[1]
        if protocol != "udp":
            self.sock.listen(8)
        self.sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                if self.protocol == "udp":
                    data, _ = self.sock.recvfrom(65535)
                    with self._lock:
                        self.datagrams.append(data)
                    continue
                conn, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def _read(self, conn: socket.socket) -> None:
        try:
            if self.context is not None:
                conn.settimeout(5)
                conn = self.context.wrap_socket(conn, server_side=True)
            conn.settimeout(5)
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                with self._lock:
                    self.raw.extend(chunk)
        except (OSError, ssl.SSLError):
            pass
        finally:
            conn.close()

    @property
    def messages(self) -> list[bytes]:
        with self._lock:
            if self.protocol == "udp":
                return list(self.datagrams)
            return parse_octet_frames(bytes(self.raw))

    def wait_for(self, count: int, timeout: float = 5.0) -> list[bytes]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                messages = self.messages
            except ValueError:  # a frame is still arriving
                messages = []
            if len(messages) >= count:
                return messages
            time.sleep(0.02)
        return self.messages

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.sock.close()


def make_tls_material(directory: Path) -> tuple[str, ssl.SSLContext]:
    """A throwaway CA and a server certificate for 127.0.0.1. Returns the CA
    PEM (for the client) and the server context."""
    now = dt.datetime.now(dt.UTC)

    def name(common: str) -> x509.Name:
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(name("NAS test CA"))
        .issuer_name(name("NAS test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(name("127.0.0.1"))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([x509.OID_SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_path = directory / "server.pem"
    key_path = directory / "server.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert_path, key_path)
    return ca_cert.public_bytes(serialization.Encoding.PEM).decode(), context
