#!/usr/bin/env python3
"""Post-install smoke test for a NAS Manager deployment (Phase 14).

Standard library only, so it runs with the system ``python3`` before or
after the virtual environment exists::

    sudo python3 deploy/smoke_test.py
    sudo python3 deploy/smoke_test.py --no-helper
    printf '%s\\n' "$PASSWORD" | sudo python3 deploy/smoke_test.py --login admin

It checks the HTTP surface (health, security headers, Host validation, no
API docs) and, unless ``--skip-system`` is given, the systemd side: the
units are active, the web and worker processes are not root and hold no
capabilities, the helper socket is ``0660 root:<group>`` and every unit has
journal entries. With ``--login`` it reads the password from stdin (never
from argv), logs in, reads ``/auth/me`` (and the disk list through the
helper) and logs out. Exit status 0 means every check passed.
"""

from __future__ import annotations

import argparse
import grp
import http.cookies
import json
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path

WEB_UNIT = "nas-manager.service"
WORKER_UNIT = "nas-manager-worker.service"
HELPER_UNIT = "nas-manager-helper.service"
REQUIRED_HEADERS = {
    "content-security-policy": None,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": None,
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
}


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    set_cookies: list[str] = field(default_factory=list)

    def json(self) -> object:
        return json.loads(self.body.decode("utf-8"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


@dataclass
class Client:
    """A tiny HTTP client with its own cookie store.

    Cookies are sent back to ``base_url`` regardless of their ``Secure``
    flag: the smoke test usually talks to the application over loopback
    HTTP while production cookies are Secure (TLS ends at the proxy).
    """

    base_url: str
    timeout: float = 10.0
    cookies: dict[str, str] = field(default_factory=dict)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: object = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        data = None if body is None else json.dumps(body).encode("utf-8")
        merged = {"Accept": "application/json", **(headers or {})}
        if data is not None:
            merged["Content-Type"] = "application/json"
        if self.cookies:
            merged["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        csrf = self.cookies.get("nas_csrf")
        if csrf and method not in {"GET", "HEAD"}:
            merged["X-CSRF-Token"] = csrf
        req = urllib.request.Request(  # noqa: S310 - the URL is the operator's
            self.base_url.rstrip("/") + path, data=data, method=method, headers=merged
        )
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                return self._response(resp.status, resp.headers, resp.read())
        except urllib.error.HTTPError as exc:
            return self._response(exc.code, exc.headers, exc.read())

    def _response(self, status: int, headers: Message, body: bytes) -> Response:
        raw = headers.get_all("set-cookie") or []
        for header in raw:
            for name, morsel in http.cookies.SimpleCookie(header).items():
                if morsel["max-age"] == "0":
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = morsel.value
        lowered = {str(k).lower(): str(v) for k, v in headers.items()}
        return Response(status, lowered, body, raw)


@dataclass
class Report:
    failures: int = 0
    lines: list[str] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        if not ok:
            self.failures += 1
        line = f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else "")
        self.lines.append(line)
        print(line, flush=True)
        return ok


# ---------------------------------------------------------------------------
# HTTP checks
# ---------------------------------------------------------------------------


def http_checks(client: Client, report: Report, *, expect_trusted_hosts: bool) -> None:
    health = client.request("GET", "/api/v1/health")
    status = health.json() if health.status == 200 else {}
    report.check(
        "health is ok",
        health.status == 200 and isinstance(status, dict) and status.get("status") == "ok",
        f"HTTP {health.status} {status.get('status') if isinstance(status, dict) else ''}",
    )

    login = client.request("GET", "/login", headers={"Accept": "text/html"})
    report.check("login page is served", login.status == 200, f"HTTP {login.status}")
    for name, expected in REQUIRED_HEADERS.items():
        value = login.headers.get(name)
        ok = value is not None and (expected is None or value == expected)
        report.check(f"header {name}", ok, value or "missing")
    report.check("no Server header", "server" not in login.headers, login.headers.get("server", ""))

    docs = client.request("GET", "/api/docs")
    report.check("API docs disabled", docs.status == 404, f"HTTP {docs.status}")

    anonymous = client.request("GET", "/api/v1/auth/me")
    report.check("anonymous /auth/me refused", anonymous.status == 401, f"HTTP {anonymous.status}")

    if expect_trusted_hosts:
        foreign = client.request("GET", "/api/v1/health", headers={"Host": "smoke-test.invalid"})
        report.check("unknown Host refused", foreign.status == 400, f"HTTP {foreign.status}")


def login_checks(
    client: Client,
    report: Report,
    username: str,
    password: str,
    *,
    helper: bool,
    expect_secure_cookie: bool = True,
) -> None:
    resp = client.request(
        "POST", "/api/v1/auth/login", body={"username": username, "password": password}
    )
    if not report.check("login", resp.status == 200, f"HTTP {resp.status}"):
        return
    session = next((c for c in resp.set_cookies if c.startswith("nas_session=")), "")
    attributes = {part.strip().lower() for part in session.split(";")[1:]}
    report.check("session cookie is HttpOnly", "httponly" in attributes)
    report.check(
        "session cookie is Secure",
        "secure" in attributes or not expect_secure_cookie,
        "not required" if not expect_secure_cookie else "",
    )
    me = client.request("GET", "/api/v1/auth/me")
    identity = me.json() if me.status == 200 else {}
    report.check(
        "/auth/me returns the user",
        isinstance(identity, dict)
        and isinstance(identity.get("user"), dict)
        and identity["user"].get("username") == username,
        f"HTTP {me.status}",
    )
    if helper:
        disks = client.request("GET", "/api/v1/disks")
        report.check(
            "disk discovery through the helper", disks.status == 200, f"HTTP {disks.status}"
        )
    out = client.request("POST", "/api/v1/auth/logout")
    report.check("logout", out.status == 204, f"HTTP {out.status}")
    after = client.request("GET", "/api/v1/auth/me")
    report.check("session revoked after logout", after.status == 401, f"HTTP {after.status}")


# ---------------------------------------------------------------------------
# systemd checks
# ---------------------------------------------------------------------------

Runner = Callable[[list[str]], str]


def run(argv: list[str]) -> str:
    result = subprocess.run(  # noqa: S603 - fixed argv
        argv, capture_output=True, text=True, timeout=30, check=False
    )
    return result.stdout.strip()


def proc_status(pid: int, proc: Path = Path("/proc")) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (proc / str(pid) / "status").read_text().splitlines():
        key, _, value = line.partition(":")
        values[key] = value.strip()
    return values


def unit_checks(
    report: Report,
    *,
    units: list[str],
    socket_path: Path,
    socket_group: str,
    runner: Runner = run,
    status_reader: Callable[[int], dict[str, str]] = proc_status,
) -> None:
    for unit in units:
        state = runner(["systemctl", "is-active", unit])
        report.check(f"{unit} active", state == "active", state)
        pid_text = runner(["systemctl", "show", "--property=MainPID", "--value", unit])
        pid = int(pid_text) if pid_text.isdigit() else 0
        if pid <= 0:
            report.check(f"{unit} main PID", False, pid_text or "none")
            continue
        status = status_reader(pid)
        uid = int(status.get("Uid", "0").split()[0])
        cap_eff = int(status.get("CapEff", "0"), 16)
        if unit == HELPER_UNIT:
            report.check(f"{unit} runs as root", uid == 0, f"uid {uid}")
        else:
            report.check(f"{unit} not root", uid != 0, f"uid {uid}")
            report.check(f"{unit} has no capabilities", cap_eff == 0, f"CapEff {cap_eff:x}")
        report.check(
            f"{unit} NoNewPrivs", status.get("NoNewPrivs") == "1", status.get("NoNewPrivs", "?")
        )
        journal = runner(["journalctl", "-u", unit, "-n", "1", "--no-pager", "-o", "cat"])
        report.check(f"{unit} has journal entries", bool(journal))
    if HELPER_UNIT in units:
        try:
            info = socket_path.stat()
        except OSError as exc:
            report.check("helper socket exists", False, str(exc))
            return
        mode = stat.S_IMODE(info.st_mode)
        group = grp.getgrgid(info.st_gid).gr_name
        report.check(
            "helper socket is 0660 root:" + socket_group,
            stat.S_ISSOCK(info.st_mode)
            and mode == 0o660
            and info.st_uid == 0
            and group == socket_group,
            f"{mode:o} uid {info.st_uid} group {group}",
        )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--no-helper", action="store_true", help="the helper is not installed")
    parser.add_argument(
        "--no-trusted-hosts",
        action="store_true",
        help="NAS_TRUSTED_HOSTS is empty (skip the unknown-Host check)",
    )
    parser.add_argument("--skip-system", action="store_true", help="HTTP checks only")
    parser.add_argument(
        "--no-secure-cookie",
        action="store_true",
        help="NAS_SESSION_COOKIE_SECURE is false (loopback-only installs)",
    )
    parser.add_argument("--socket", default="/run/nas-manager/helper.sock")
    parser.add_argument("--socket-group", default="nas-manager")
    parser.add_argument("--login", metavar="USERNAME", help="password is read from stdin")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = Report()
    client = Client(args.base_url)
    try:
        http_checks(client, report, expect_trusted_hosts=not args.no_trusted_hosts)
        if args.login:
            password = sys.stdin.readline().rstrip("\n")
            login_checks(
                client,
                report,
                args.login,
                password,
                helper=not args.no_helper,
                expect_secure_cookie=not args.no_secure_cookie,
            )
    except (urllib.error.URLError, OSError) as exc:
        report.check("application reachable", False, str(exc))
    if not args.skip_system:
        units = [WEB_UNIT, WORKER_UNIT] + ([] if args.no_helper else [HELPER_UNIT])
        unit_checks(
            report, units=units, socket_path=Path(args.socket), socket_group=args.socket_group
        )
    total = len(report.lines)
    print(f"\n{total - report.failures}/{total} checks passed", flush=True)
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
