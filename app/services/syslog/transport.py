"""Syslog transports: UDP (RFC 5426), TCP (RFC 6587) and TLS (RFC 5425).

Framing: UDP sends one message per datagram, truncated to 2048 bytes (the
size every receiver must accept). TCP and TLS use octet counting for
RFC 5424 messages and newline termination for RFC 3164 ones. TLS always
verifies the server certificate and host name (system trust store, or the
destination's CA certificate when one is set); there is no insecure mode.

Delivery over TCP/TLS means the kernel accepted the bytes on a connection
that was open: plain syslog has no application-level acknowledgement, so a
collector that crashes mid-batch can lose the batch (see docs/SECURITY.md).
"""

from __future__ import annotations

import contextlib
import socket
import ssl
from collections.abc import Iterable

from app.models.syslog_destination import FORMAT_RFC3164, PROTOCOL_TLS, PROTOCOL_UDP

UDP_MAX_BYTES = 2048


class SyslogTransportError(Exception):
    """The destination could not be reached or refused the data."""


def frame(payload: bytes, *, protocol: str, fmt: str) -> bytes:
    if protocol == PROTOCOL_UDP:
        if len(payload) <= UDP_MAX_BYTES:
            return payload
        # Cut on a UTF-8 character boundary.
        return payload[:UDP_MAX_BYTES].decode("utf-8", "ignore").encode()
    if fmt == FORMAT_RFC3164:
        return payload.replace(b"\n", b" ") + b"\n"
    return str(len(payload)).encode() + b" " + payload


def tls_context(ca_pem: str | None) -> ssl.SSLContext:
    """Verified TLS 1.2+ client context; ``ca_pem`` replaces the system store."""
    context = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class SyslogSender:
    """One destination's connection. TCP/TLS connections are opened lazily,
    kept between sends and re-opened after an error."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        protocol: str,
        ca_pem: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.protocol = protocol
        self.timeout = timeout
        self._context = tls_context(ca_pem) if protocol == PROTOCOL_TLS else None
        self._sock: socket.socket | None = None

    def _connect(self) -> socket.socket:
        if self.protocol == PROTOCOL_UDP:
            infos = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_DGRAM)
            family, kind, proto, _, address = infos[0]
            sock = socket.socket(family, kind, proto)
            sock.settimeout(self.timeout)
            sock.connect(address)
            return sock
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self._context is not None:
            try:
                return self._context.wrap_socket(sock, server_hostname=self.host)
            except BaseException:
                sock.close()
                raise
        return sock

    def send(self, frames: Iterable[bytes]) -> None:
        try:
            if self._sock is None:
                self._sock = self._connect()
            for data in frames:
                if self.protocol == PROTOCOL_UDP:
                    self._sock.send(data)
                else:
                    self._sock.sendall(data)
        except (OSError, ssl.SSLError, ValueError) as exc:
            self.close()
            raise SyslogTransportError(describe_error(exc)) from exc

    def close(self) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"TLS certificate verification failed: {exc.verify_message or exc}"
    if isinstance(exc, socket.gaierror):
        return f"host name lookup failed: {exc.strerror or exc}"
    if isinstance(exc, TimeoutError | socket.timeout):
        return "connection timed out"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc) or exc.__class__.__name__
