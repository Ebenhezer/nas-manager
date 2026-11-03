"""Remote providers: real SFTP over loopback, mocked HTTP (WebDAV, Drive),
botocore Stubber (S3) and a fake ftplib (FTP/FTPS). No external network."""

from __future__ import annotations

import ftplib
import io
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, unquote, urlsplit

import boto3
import httpx2
import paramiko
import pytest
from botocore.stub import Stubber

from app.core.config import Settings
from app.core.errors import BadRequestError, RemoteError
from app.services.remote import registry
from app.services.remote.base import normalize_remote_path
from app.services.remote.ftp import FtpProvider, parse_modify
from app.services.remote.gdrive import (
    FOLDER_MIME,
    GoogleDriveProvider,
    authorization_url,
    exchange_code,
)
from app.services.remote.s3 import S3Provider
from app.services.remote.sftp import SftpProvider, fingerprint, probe_host_key
from app.services.remote.webdav import WebDavProvider
from tests.sftp_stub_server import SftpTestServer, generate_ed25519

# ---------------------------------------------------------------------------
# Paths and validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", ""), ("/backups/", "backups"), ("a//b", "a/b"), ("photos/2026", "photos/2026")],
)
def test_normalize_remote_path(raw: str, expected: str) -> None:
    assert normalize_remote_path(raw) == expected


@pytest.mark.parametrize("raw", ["../etc", "a/../../b", "a\\b", "x\x00", "a/./b"])
def test_normalize_remote_path_rejects_traversal(raw: str) -> None:
    with pytest.raises(BadRequestError):
        normalize_remote_path(raw)


def test_registry_requires_tls_or_explicit_insecure() -> None:
    with pytest.raises(BadRequestError, match="unencrypted"):
        registry.validate_config("ftp", {"host": "nas", "username": "u"}, {"password": "p"}, {})
    config, _ = registry.validate_config(
        "ftp", {"host": "nas", "username": "u", "allow_insecure": True}, {"password": "p"}, {}
    )
    assert config["allow_insecure"] is True
    with pytest.raises(BadRequestError, match="plain HTTP"):
        registry.validate_config("webdav", {"url": "http://dav.local/"}, {}, {})
    with pytest.raises(BadRequestError, match="credentials"):
        registry.validate_config("webdav", {"url": "https://u:p@dav.local/"}, {}, {})


def test_registry_keeps_secrets_out_of_config() -> None:
    with pytest.raises(BadRequestError, match="secret"):
        registry.validate_config("webdav", {"url": "https://x/", "password": "p"}, {}, {})
    with pytest.raises(BadRequestError, match="Unknown"):
        registry.validate_config("webdav", {"url": "https://x/", "evil": 1}, {}, {})
    with pytest.raises(BadRequestError, match="set by the server"):
        registry.validate_config(
            "gdrive", {"client_id": "id"}, {"client_secret": "s", "refresh_token": "r"}, {}
        )


def test_registry_patch_semantics_for_secrets() -> None:
    existing = {"password": "old"}
    cfg = {"url": "https://x/", "username": "u"}
    _, kept = registry.validate_config("webdav", cfg, {}, existing)
    assert kept == {"password": "old"}
    _, replaced = registry.validate_config("webdav", cfg, {"password": "new"}, existing)
    assert replaced == {"password": "new"}
    _, cleared = registry.validate_config("webdav", cfg, {"password": None}, existing)
    assert cleared == {}


def test_registry_validates_sftp_and_s3() -> None:
    _key, pem = generate_ed25519()
    with pytest.raises(BadRequestError, match="password or a private key"):
        registry.validate_config(
            "sftp", {"host": "h", "username": "u", "host_key": _host_line()}, {}, {}
        )
    with pytest.raises(BadRequestError, match="host_key"):
        registry.validate_config(
            "sftp", {"host": "h", "username": "u", "host_key": "garbage"}, {"password": "p"}, {}
        )
    config, secrets = registry.validate_config(
        "sftp",
        {"host": "h", "username": "u", "host_key": _host_line(), "base_path": "/srv/x/"},
        {"private_key": pem},
        {},
    )
    assert config["base_path"] == "/srv/x"
    assert config["port"] == 22
    assert "private_key" in secrets
    with pytest.raises(BadRequestError, match="host"):
        registry.validate_config(
            "sftp",
            {"host": "-oProxyCommand=x", "username": "u", "host_key": _host_line()},
            {"password": "p"},
            {},
        )
    with pytest.raises(BadRequestError, match="bucket"):
        registry.validate_config("s3", {"bucket": "Bad_Bucket"}, {}, {})
    with pytest.raises(BadRequestError, match="both"):
        registry.validate_config("s3", {"bucket": "ok-bucket", "access_key_id": "AK"}, {}, {})


def test_specs_payload_hides_managed_secret() -> None:
    gdrive = next(p for p in registry.specs_payload() if p["name"] == "gdrive")
    assert "refresh_token" not in [f["name"] for f in gdrive["fields"]]
    assert gdrive["oauth"] is True


def _host_line() -> str:
    key, _ = generate_ed25519()
    return f"{key.get_name()} {key.get_base64()}"


# ---------------------------------------------------------------------------
# SFTP against a real in-process server
# ---------------------------------------------------------------------------


@pytest.fixture
def sftp_server(tmp_path: Path) -> Iterator[SftpTestServer]:
    root = tmp_path / "sftp-root"
    root.mkdir()
    with SftpTestServer(root) as server:
        yield server


def test_sftp_round_trip(sftp_server: SftpTestServer) -> None:
    (sftp_server.root / "data").mkdir()
    (sftp_server.root / "data" / "hello.txt").write_text("hello")
    provider = SftpProvider(
        sftp_server.config(base_path="/data"), {"password": "s3cret"}, timeout=5
    )
    with provider:
        assert "1 entries" in provider.test()
        provider.makedirs("deep/er")
        provider.upload(io.BytesIO(b"payload"), "deep/er/file.bin")
        walked = provider.walk("")
        assert set(walked.files) == {"hello.txt", "deep/er/file.bin"}
        out = io.BytesIO()
        provider.download("deep/er/file.bin", out)
        assert out.getvalue() == b"payload"
        stat = provider.stat("deep/er/file.bin")
        assert stat is not None and stat.size == 7
        assert provider.stat("missing") is None
        assert provider.dir_exists("deep/er") and not provider.dir_exists("hello.txt")
        assert not provider.dir_exists("nope")
        provider.makedirs("archive")
        provider.move("hello.txt", "archive/hello.txt")
    assert (sftp_server.root / "data" / "archive" / "hello.txt").read_text() == "hello"
    assert not list(sftp_server.root.rglob(".nas-remote-*"))


def test_sftp_rejects_wrong_host_key(sftp_server: SftpTestServer) -> None:
    provider = SftpProvider(
        sftp_server.config(host_key=_host_line()), {"password": "s3cret"}, timeout=5
    )
    with pytest.raises(RemoteError) as excinfo:
        provider.connect()
    assert "host key" in excinfo.value.message.lower()


def test_sftp_bad_password(sftp_server: SftpTestServer) -> None:
    with pytest.raises(RemoteError, match="authentication failed"):
        SftpProvider(sftp_server.config(), {"password": "wrong"}, timeout=5).connect()


def test_sftp_probe_host_key(sftp_server: SftpTestServer) -> None:
    probed = probe_host_key("127.0.0.1", sftp_server.port, timeout=5)
    assert probed["host_key"] == sftp_server.host_key_line
    assert probed["fingerprint"] == fingerprint(sftp_server.host_key)


def test_sftp_unreachable() -> None:
    with pytest.raises(RemoteError):
        probe_host_key("127.0.0.1", 1, timeout=2)


def test_sftp_private_key_loading() -> None:
    _, pem = generate_ed25519()
    provider = SftpProvider(
        {"host": "h", "username": "u", "host_key": _host_line()}, {"private_key": pem}, timeout=1
    )
    assert isinstance(provider.pkey, paramiko.Ed25519Key)
    with pytest.raises(BadRequestError):
        SftpProvider(
            {"host": "h", "username": "u", "host_key": _host_line()},
            {"private_key": "not a key"},
            timeout=1,
        )


# ---------------------------------------------------------------------------
# WebDAV (mocked HTTP)
# ---------------------------------------------------------------------------


def _multistatus(*items: tuple[str, bool, int, str]) -> bytes:
    parts = []
    for href, is_dir, size, etag in items:
        kind = "<d:collection/>" if is_dir else ""
        parts.append(
            f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>"
            f"<d:resourcetype>{kind}</d:resourcetype>"
            f"<d:getcontentlength>{size}</d:getcontentlength>"
            "<d:getlastmodified>Mon, 02 Nov 2026 10:00:00 GMT</d:getlastmodified>"
            f"<d:getetag>{etag}</d:getetag>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )
    return ('<d:multistatus xmlns:d="DAV:">' + "".join(parts) + "</d:multistatus>").encode()


class DavServer:
    """Records requests and serves a tiny fixed tree under /dav/."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.stored: dict[str, bytes] = {}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        path = unquote(request.url.path)
        self.requests.append((request.method, path, dict(request.headers)))
        if request.headers.get("authorization") is None:
            return httpx2.Response(401)
        if request.method == "PROPFIND":
            if path in ("/dav/", "/dav"):
                body = _multistatus(
                    ("/dav/", True, 0, ""),
                    ("/dav/a%20b.txt", False, 3, '"e1"'),
                    ("/dav/sub/", True, 0, ""),
                    ("/dav/%2E%2E/", True, 0, ""),
                )
                return httpx2.Response(207, content=body)
            if path == "/dav/sub/":
                return httpx2.Response(207, content=_multistatus(("/dav/sub/", True, 0, "")))
            if path == "/dav/a b.txt":
                return httpx2.Response(
                    207, content=_multistatus(("/dav/a%20b.txt", False, 3, '"e1"'))
                )
            return httpx2.Response(404)
        if request.method == "GET":
            return httpx2.Response(200, content=b"abc")
        if request.method == "PUT":
            self.stored[path] = request.read()
            return httpx2.Response(201)
        if request.method == "MOVE":
            destination = unquote(urlsplit(request.headers["destination"]).path)
            self.stored[destination] = self.stored.pop(path, b"")
            return httpx2.Response(201)
        if request.method == "MKCOL":
            return httpx2.Response(405 if path == "/dav/sub/" else 201)
        return httpx2.Response(500)


def _dav(server: DavServer, password: str = "pw") -> WebDavProvider:
    return WebDavProvider(
        {"url": "https://dav.example/dav/", "username": "me"},
        {"password": password},
        timeout=5,
        transport=httpx2.MockTransport(server),
    )


def test_webdav_walk_download_upload() -> None:
    server = DavServer()
    with _dav(server) as provider:
        walked = provider.walk("")
        assert set(walked.files) == {"a b.txt"}
        assert walked.files["a b.txt"].etag == '"e1"'
        assert [reason for _, reason in walked.rejected] == ["empty or dot name"]
        out = io.BytesIO()
        provider.download("a b.txt", out)
        assert out.getvalue() == b"abc"
        provider.makedirs("sub/new")
        provider.upload(io.BytesIO(b"data"), "sub/new/x.txt")
        stat = provider.stat("a b.txt")
        assert stat is not None and stat.size == 3
        assert provider.stat("nope") is None
        assert provider.dir_exists("sub") and not provider.dir_exists("nope")
    methods = [(m, p) for m, p, _ in server.requests if m in {"PUT", "MOVE", "MKCOL"}]
    assert methods[0] == ("MKCOL", "/dav/sub/")
    assert methods[2][0] == "PUT" and "/.nas-remote-" in methods[2][1]
    assert methods[3][0] == "MOVE"
    assert server.stored == {"/dav/sub/new/x.txt": b"data"}
    assert server.requests[0][2]["authorization"].startswith("Basic ")


def test_webdav_auth_failure() -> None:
    def deny(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401)

    provider = WebDavProvider(
        {"url": "https://dav.example/dav/"}, {}, timeout=5, transport=httpx2.MockTransport(deny)
    )
    with provider, pytest.raises(RemoteError, match="authentication"):
        provider.test()


def test_webdav_malformed_xml() -> None:
    def broken(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(207, content=b"<not-xml")

    provider = WebDavProvider(
        {"url": "https://dav.example/dav/"}, {}, timeout=5, transport=httpx2.MockTransport(broken)
    )
    with provider, pytest.raises(RemoteError, match="malformed"):
        provider.test()


# ---------------------------------------------------------------------------
# S3 (botocore Stubber)
# ---------------------------------------------------------------------------


# botocore reads its (large) service models from disk on first use; one
# shared session keeps that cost to once per test run.
_BOTO_SESSION = boto3.session.Session()


def _s3() -> tuple[S3Provider, Stubber, Any]:
    client = _BOTO_SESSION.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="AKIDEXAMPLE",
        aws_secret_access_key="secret",
    )
    stubber = Stubber(client)
    provider = S3Provider({"bucket": "my-bucket", "prefix": "nas"}, {}, timeout=5, client=client)
    return provider, stubber, client


def test_s3_walk_filters_and_reports() -> None:
    provider, stubber, _ = _s3()
    when = datetime(2026, 11, 2, tzinfo=UTC)
    stubber.add_response(
        "list_objects_v2",
        {
            "IsTruncated": False,
            "Contents": [
                {
                    "Key": "nas/job/a.txt",
                    "Size": 3,
                    "ETag": '"900150983cd24fb0d6963f7d28e17f72"',
                    "LastModified": when,
                },
                {"Key": "nas/job/dir/", "Size": 0, "ETag": '"x"', "LastModified": when},
                {"Key": "nas/job/dir/b.bin", "Size": 9, "ETag": '"abc-2"', "LastModified": when},
                {
                    "Key": "nas/job/.nas-sync-archive/old",
                    "Size": 1,
                    "ETag": '"y"',
                    "LastModified": when,
                },
                {"Key": "nas/job/x/../../escape", "Size": 1, "ETag": '"z"', "LastModified": when},
            ],
        },
        {"Bucket": "my-bucket", "Prefix": "nas/job/"},
    )
    with stubber, provider:
        walked = provider.walk("job")
    assert set(walked.files) == {"a.txt", "dir/b.bin"}
    assert walked.files["a.txt"].md5 == "900150983cd24fb0d6963f7d28e17f72"
    assert walked.files["dir/b.bin"].md5 is None  # multipart ETag is not an MD5
    assert len(walked.rejected) == 1


def test_s3_dir_exists_checks_the_prefix() -> None:
    provider, stubber, _ = _s3()
    when = datetime(2026, 1, 1, tzinfo=UTC)
    stubber.add_response(
        "list_objects_v2",
        {"IsTruncated": False, "Contents": [{"Key": "nas/d/x", "Size": 1, "LastModified": when}]},
        {"Bucket": "my-bucket", "Prefix": "nas/d/", "MaxKeys": 1},
    )
    stubber.add_response(
        "list_objects_v2",
        {"IsTruncated": False},
        {"Bucket": "my-bucket", "Prefix": "nas/e/", "MaxKeys": 1},
    )
    with stubber, provider:
        assert provider.dir_exists("d") is True
        assert provider.dir_exists("e") is False
        assert provider.dir_exists("") is True


def test_s3_stat_and_move() -> None:
    provider, stubber, _ = _s3()
    stubber.add_client_error("head_object", http_status_code=404, service_error_code="404")
    stubber.add_response(
        "head_object",
        {"ContentLength": 3, "ETag": '"e"', "LastModified": datetime(2026, 1, 1, tzinfo=UTC)},
        {"Bucket": "my-bucket", "Key": "nas/a.txt"},
    )
    stubber.add_client_error("head_object", http_status_code=404, service_error_code="404")
    stubber.add_response("delete_object", {}, {"Bucket": "my-bucket", "Key": "nas/a.txt"})
    with stubber, provider:
        assert provider.stat("missing") is None
        entry = provider.stat("a.txt")
        assert entry is not None and entry.size == 3
        copied: list[tuple[Any, ...]] = []
        provider.client.copy = lambda *args, **kwargs: copied.append(args)
        provider.move("a.txt", "archive/a.txt")
    assert copied == [
        ({"Bucket": "my-bucket", "Key": "nas/a.txt"}, "my-bucket", "nas/archive/a.txt")
    ]


def test_s3_errors_become_remote_errors() -> None:
    provider, stubber, _ = _s3()
    stubber.add_client_error("head_bucket", http_status_code=403, service_error_code="AccessDenied")
    with stubber, provider, pytest.raises(RemoteError, match="AccessDenied"):
        provider.test()


def test_s3_transfers_use_the_full_key() -> None:
    calls: list[tuple[str, str]] = []

    class FakeClient:
        def upload_fileobj(self, source: io.BytesIO, bucket: str, key: str) -> None:
            calls.append(("up", key))

        def download_fileobj(self, bucket: str, key: str, out: io.BytesIO) -> None:
            calls.append(("down", key))
            out.write(b"x")

    provider = S3Provider({"bucket": "b-1", "prefix": "p"}, {}, timeout=5, client=FakeClient())  # type: ignore[arg-type]
    with provider:
        provider.upload(io.BytesIO(b"x"), "d/f")
        provider.download("d/f", io.BytesIO())
    assert calls == [("up", "p/d/f"), ("down", "p/d/f")]


# ---------------------------------------------------------------------------
# FTP / FTPS (fake ftplib)
# ---------------------------------------------------------------------------


class FakeFTP:
    instances: ClassVar[list[FakeFTP]] = []
    mlsd_supported = True
    tls_class = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.commands: list[str] = []
        self.files: dict[str, bytes] = {"/base/a.txt": b"hello"}
        self.dirs = {"/base", "/base/sub"}
        self.protected = False
        FakeFTP.instances.append(self)

    def connect(self, host: str, port: int, timeout: float) -> None:
        self.commands.append(f"CONNECT {host}:{port}")

    def login(self, user: str, password: str) -> None:
        if password != "pw":
            raise ftplib.error_perm("530 Login incorrect")
        self.commands.append(f"USER {user}")

    def prot_p(self) -> None:
        self.protected = True

    def set_pasv(self, value: bool) -> None:
        self.commands.append(f"PASV {value}")

    def mlsd(self, path: str, facts: list[str]) -> Iterator[tuple[str, dict[str, str]]]:
        if not self.mlsd_supported:
            raise ftplib.error_perm("500 Unknown command")
        prefix = path.rstrip("/") + "/"
        yield ".", {"type": "cdir"}
        for name in sorted(
            {
                p[len(prefix) :].split("/")[0]
                for p in self.files | dict.fromkeys(self.dirs)
                if p.startswith(prefix)
            }
        ):
            full = prefix + name
            if full in self.dirs:
                yield name, {"type": "dir", "modify": "20261102100000"}
            elif full in self.files:
                yield (
                    name,
                    {
                        "type": "file",
                        "size": str(len(self.files[full])),
                        "modify": "20261102100000.123",
                    },
                )

    def retrbinary(self, cmd: str, callback: Callable[[bytes], Any]) -> None:
        callback(self.files[cmd.split(" ", 1)[1]])

    def storbinary(self, cmd: str, source: io.BytesIO) -> None:
        self.files[cmd.split(" ", 1)[1]] = source.read()

    def rename(self, old: str, new: str) -> None:
        if new in self.files:
            raise ftplib.error_perm("553 exists")
        self.files[new] = self.files.pop(old)

    def delete(self, path: str) -> None:
        self.files.pop(path)

    def mkd(self, path: str) -> None:
        self.dirs.add(path)

    def quit(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeFTPTLS(FakeFTP):
    pass


@pytest.fixture
def fake_ftp(monkeypatch: pytest.MonkeyPatch) -> type[FakeFTP]:
    FakeFTP.instances = []
    FakeFTP.mlsd_supported = True
    monkeypatch.setattr(ftplib, "FTP", FakeFTP)
    monkeypatch.setattr(ftplib, "FTP_TLS", FakeFTPTLS)
    return FakeFTP


def _ftp(tls: bool, password: str = "pw") -> FtpProvider:
    return FtpProvider(
        {"host": "ftp.local", "username": "u", "base_path": "/base"},
        {"password": password},
        timeout=5,
        tls=tls,
    )


def test_ftps_round_trip(fake_ftp: type[FakeFTP]) -> None:
    with _ftp(tls=True) as provider:
        walked = provider.walk("")
        assert set(walked.files) == {"a.txt"}
        assert walked.files["a.txt"].mtime == datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
        provider.upload(io.BytesIO(b"new"), "a.txt")  # rename over existing falls back
        out = io.BytesIO()
        provider.download("a.txt", out)
        assert out.getvalue() == b"new"
        provider.makedirs("sub/x")
        provider.move("a.txt", "sub/x/a.txt")
    server = fake_ftp.instances[0]
    assert server.protected is True
    assert "/base/sub/x" in server.dirs
    assert not [p for p in server.files if ".nas-remote-" in p]


def test_ftp_login_and_mlsd_errors(fake_ftp: type[FakeFTP]) -> None:
    with pytest.raises(RemoteError, match="login refused"):
        _ftp(tls=False, password="bad").connect()
    fake_ftp.mlsd_supported = False
    with _ftp(tls=False) as provider, pytest.raises(RemoteError, match="MLSD"):
        provider.test()


def test_parse_modify() -> None:
    assert parse_modify("20261102100000") == datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    assert parse_modify("garbage") is None
    assert parse_modify(None) is None


# ---------------------------------------------------------------------------
# Google Drive (mocked HTTP)
# ---------------------------------------------------------------------------


class DriveServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.token_calls = 0
        self.reject_first_api_call = False
        self.items: list[dict[str, Any]] = [
            {
                "id": "f1",
                "name": "a.txt",
                "mimeType": "text/plain",
                "size": "3",
                "modifiedTime": "2026-11-02T10:00:00.000Z",
                "md5Checksum": "abc",
            },
            {"id": "f2", "name": "dup", "mimeType": "text/plain", "size": "1"},
            {"id": "f3", "name": "dup", "mimeType": "text/plain", "size": "1"},
            {"id": "f4", "name": "Notes", "mimeType": "application/vnd.google-apps.document"},
            {"id": "d1", "name": "sub", "mimeType": FOLDER_MIME},
        ]
        self.uploaded: dict[str, bytes] = {}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        self.requests.append((request.method, str(url.path)))
        if url.host == "oauth2.googleapis.com":
            self.token_calls += 1
            form = parse_qs(request.read().decode())
            if form.get("grant_type") == ["authorization_code"]:
                if form.get("code") != ["good"]:
                    return httpx2.Response(400, json={"error": "invalid_grant"})
                return httpx2.Response(200, json={"refresh_token": "r-1", "access_token": "a"})
            return httpx2.Response(
                200, json={"access_token": f"tok{self.token_calls}", "expires_in": 3600}
            )
        if self.reject_first_api_call:
            self.reject_first_api_call = False
            return httpx2.Response(401)
        params = parse_qs(url.query.decode() if isinstance(url.query, bytes) else str(url.query))
        if url.path == "/drive/v3/files" and request.method == "GET":
            query = params["q"][0]
            if "'root' in parents" in query:
                items = self.items
            elif "'d1' in parents" in query:
                items = []
            else:
                items = []
            if " and name = '" in query:
                name = query.split(" and name = '")[1].rstrip("'")
                items = [i for i in items if i["name"] == name]
            return httpx2.Response(200, json={"files": items})
        if url.path == "/upload/drive/v3/files" and request.method == "POST":
            meta = json.loads(request.read())
            return httpx2.Response(
                200, headers={"location": f"https://upload.example/session/{meta['name']}"}
            )
        if url.host == "upload.example":
            self.uploaded[url.path] = request.read()
            return httpx2.Response(200, json={"id": "new1"})
        if url.path.startswith("/drive/v3/files/") and params.get("alt") == ["media"]:
            return httpx2.Response(200, content=b"abc")
        return httpx2.Response(404)


def _drive(server: DriveServer) -> GoogleDriveProvider:
    return GoogleDriveProvider(
        {"client_id": "cid"},
        {"client_secret": "cs", "refresh_token": "rt"},
        timeout=5,
        transport=httpx2.MockTransport(server),
    )


def test_drive_walk_reports_duplicates_and_native_docs() -> None:
    server = DriveServer()
    with _drive(server) as provider:
        walked = provider.walk("")
    assert set(walked.files) == {"a.txt"}
    assert walked.files["a.txt"].md5 == "abc"
    reasons = sorted(reason for _, reason in walked.rejected)
    assert reasons[0].startswith("Google-native")
    assert reasons[1:] == ["several Drive items share this name"] * 2
    assert server.token_calls == 1  # access token cached


def test_drive_upload_and_download() -> None:
    server = DriveServer()
    with _drive(server) as provider:
        provider.upload(io.BytesIO(b"hello"), "sub/new.txt")
        out = io.BytesIO()
        provider.download("a.txt", out)
    assert server.uploaded == {"/session/new.txt": b"hello"}
    assert out.getvalue() == b"abc"


def test_drive_refreshes_token_on_401() -> None:
    server = DriveServer()
    server.reject_first_api_call = True
    with _drive(server) as provider:
        provider.test()
    assert server.token_calls == 2


def test_drive_requires_authorization() -> None:
    provider = GoogleDriveProvider({"client_id": "c"}, {"client_secret": "s"}, timeout=5)
    with pytest.raises(RemoteError, match="not authorized"):
        provider.connect()


def test_drive_oauth_helpers() -> None:
    url = authorization_url(
        client_id="cid", redirect_uri="https://nas/cb", state="1.x", challenge="ch"
    )
    query = parse_qs(urlsplit(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["access_type"] == ["offline"]
    server = DriveServer()
    transport = httpx2.MockTransport(server)
    token = exchange_code(
        client_id="cid",
        client_secret="cs",
        code="good",
        verifier="v",
        redirect_uri="https://nas/cb",
        timeout=5,
        transport=transport,
    )
    assert token == "r-1"
    with pytest.raises(RemoteError, match="refused"):
        exchange_code(
            client_id="cid",
            client_secret="cs",
            code="bad",
            verifier="v",
            redirect_uri="https://nas/cb",
            timeout=5,
            transport=transport,
        )


def test_open_provider_builds_each_kind(settings: Settings) -> None:
    host_line = _host_line()
    assert isinstance(
        registry.open_provider(
            "sftp",
            {"host": "h", "username": "u", "host_key": host_line},
            {"password": "p"},
            settings,
        ),
        SftpProvider,
    )
    assert isinstance(registry.open_provider("ftps", {"host": "h"}, {}, settings), FtpProvider)
    assert isinstance(
        registry.open_provider("webdav", {"url": "https://x/"}, {}, settings), WebDavProvider
    )
    assert isinstance(registry.open_provider("s3", {"bucket": "b-1"}, {}, settings), S3Provider)
    assert isinstance(
        registry.open_provider("gdrive", {"client_id": "c"}, {}, settings), GoogleDriveProvider
    )
    with pytest.raises(BadRequestError):
        registry.open_provider("nfs", {}, {}, settings)
