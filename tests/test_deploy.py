"""Phase 14: deployment artifacts (systemd units, scripts, smoke test)."""

from __future__ import annotations

import importlib.util
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import uvicorn
from fastapi import FastAPI

from app.db.migrations import PROJECT_ROOT

from .conftest import ADMIN_PASSWORD

DEPLOY = PROJECT_ROOT / "deploy"
UNITS = DEPLOY / "systemd"
SCRIPTS = ["install.sh", "uninstall.sh", "nas-manager-cli"]
HELPER_CAPABILITIES = {
    "CAP_SYS_ADMIN",
    "CAP_MKNOD",
    "CAP_RAWIO",
    "CAP_DAC_OVERRIDE",
    "CAP_CHOWN",
    "CAP_FOWNER",
    "CAP_SETGID",
}


def parse_unit(path: Path) -> dict[str, list[str]]:
    """``Section.Key`` -> every value (systemd allows repeated keys)."""
    values: dict[str, list[str]] = {}
    section = ""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, _, value = line.partition("=")
        values.setdefault(f"{section}.{key.strip()}", []).append(value.strip())
    return values


def one(unit: dict[str, list[str]], key: str) -> str:
    assert key in unit, key
    return unit[key][-1]


@pytest.mark.parametrize("name", ["nas-manager.service", "nas-manager-worker.service"])
def test_unprivileged_units_are_sandboxed(name: str) -> None:
    unit = parse_unit(UNITS / name)
    assert one(unit, "Service.User") == "nas-manager"
    assert one(unit, "Service.Group") == "nas-manager"
    assert one(unit, "Service.CapabilityBoundingSet") == ""
    assert one(unit, "Service.AmbientCapabilities") == ""
    for key, expected in {
        "NoNewPrivileges": "true",
        "ProtectSystem": "strict",
        "ProtectHome": "true",
        "PrivateTmp": "true",
        "PrivateDevices": "true",
        "DevicePolicy": "closed",
        "RestrictSUIDSGID": "true",
        "MemoryDenyWriteExecute": "true",
        "RemoveIPC": "true",
        "SystemCallErrorNumber": "EPERM",
        "UMask": "0027",
    }.items():
        assert one(unit, f"Service.{key}") == expected, key
    assert "~@privileged @resources" in unit["Service.SystemCallFilter"]
    env = unit["Service.Environment"]
    assert "NAS_DATA_DIR=/var/lib/nas-manager" in env
    assert "NAS_ENVIRONMENT=production" in env


def test_helper_unit_is_restricted_root() -> None:
    unit = parse_unit(UNITS / "nas-manager-helper.service")
    assert "Service.User" not in unit  # root by design
    caps = set(one(unit, "Service.CapabilityBoundingSet").split())
    assert caps == HELPER_CAPABILITIES
    assert one(unit, "Service.WorkingDirectory") == "/opt/nas-manager"
    assert one(unit, "Service.RestrictAddressFamilies") == "AF_UNIX AF_NETLINK"
    assert one(unit, "Service.IPAddressDeny") == "any"
    assert one(unit, "Service.NoNewPrivileges") == "true"
    # Mounts must land in the host's mount namespace.
    for key in (
        "PrivateTmp",
        "ProtectHome",
        "ProtectSystem",
        "ProtectKernelModules",
        "ProtectKernelLogs",
        "ProtectKernelTunables",
        "ProtectControlGroups",
        "PrivateDevices",
        "ReadOnlyPaths",
        "ReadWritePaths",
        "InaccessiblePaths",
        "MountFlags",
    ):
        assert f"Service.{key}" not in unit, key
    # The service account must be able to traverse to the socket.
    assert one(unit, "Service.RuntimeDirectoryMode") == "0711"
    # mount/umount are not part of @system-service.
    assert "@system-service @mount" in unit["Service.SystemCallFilter"]
    env = unit["Service.Environment"]
    assert "NAS_DATA_DIR=/var/lib/nas-manager" in env
    assert "NAS_LOG_TO_FILE=false" in env
    assert "NAS_SYSLOG_IDENTIFIER=nas-manager-helper" in env


@pytest.mark.parametrize("path", sorted(UNITS.glob("*.service")), ids=lambda p: p.name)
def test_units_exec_without_shell(path: Path) -> None:
    unit = parse_unit(path)
    commands = unit.get("Service.ExecStart", []) + unit.get("Service.ExecStartPre", [])
    assert commands
    for command in commands:
        assert command.startswith("/opt/nas-manager/.venv/bin/"), command
        assert not any(token in command for token in ("sh -c", ";", "&&", "|", "`", "$("))


def test_web_unit_is_ordered_after_the_helper() -> None:
    web = parse_unit(UNITS / "nas-manager.service")
    worker = parse_unit(UNITS / "nas-manager-worker.service")
    assert "nas-manager-helper.service" in one(web, "Unit.After")
    assert "nas-manager.service" in one(worker, "Unit.After")


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze missing")
@pytest.mark.parametrize("path", sorted(UNITS.glob("*.service")), ids=lambda p: p.name)
def test_systemd_analyze_verify(path: Path) -> None:
    result = subprocess.run(  # noqa: S603
        ["systemd-analyze", "verify", str(path)],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    problems = [
        line
        for line in (result.stdout + result.stderr).splitlines()
        if line.startswith(path.name)
        # The application is not installed in this workspace.
        and "/opt/nas-manager/.venv/bin/" not in line
    ]
    assert problems == []


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_are_valid_bash(name: str) -> None:
    script = DEPLOY / name
    assert script.read_text().startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in script.read_text()
    subprocess.run(["bash", "-n", str(script)], check=True, timeout=30)  # noqa: S603,S607


def test_uninstall_purge_requires_confirmation() -> None:
    text = (DEPLOY / "uninstall.sh").read_text()
    assert "$PURGE && ! $YES" in text
    # Only fixed, guarded paths are ever removed.
    for line in text.splitlines():
        if "rm -rf" in line:
            assert ":?}" in line, line


def test_installer_never_overwrites_the_env_file() -> None:
    text = (DEPLOY / "install.sh").read_text()
    assert 'if [[ -e $ENV_FILE ]]; then\n    log "keeping the existing $ENV_FILE"' in text


def test_nginx_example_overwrites_forwarded_for() -> None:
    text = (DEPLOY / "nginx" / "nas-manager.conf").read_text()
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in text
    assert "$proxy_add_x_forwarded_for" not in text
    assert "location = /api/v1/public/uploads" in text


# --- deploy/smoke_test.py ---------------------------------------------------


@pytest.fixture(scope="module")
def smoke() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smoke_test", DEPLOY / "smoke_test.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_test"] = module
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture
def live_server(app: FastAPI, admin_user: object) -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, server_header=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=15)


def test_smoke_test_http_checks_pass(smoke: ModuleType, live_server: str) -> None:
    report = smoke.Report()
    client = smoke.Client(live_server)
    smoke.http_checks(client, report, expect_trusted_hosts=False)
    smoke.login_checks(
        client, report, "admin", ADMIN_PASSWORD, helper=False, expect_secure_cookie=False
    )
    assert report.failures == 0, report.lines
    # The test app does not mark cookies Secure; a production check flags it.
    strict = smoke.Report()
    smoke.login_checks(smoke.Client(live_server), strict, "admin", ADMIN_PASSWORD, helper=False)
    assert [line for line in strict.lines if line.startswith("FAIL")] == [
        "FAIL  session cookie is Secure"
    ]
    names = " ".join(report.lines)
    assert "login" in names and "session revoked after logout" in names


def test_smoke_test_reports_failures(smoke: ModuleType, live_server: str) -> None:
    report = smoke.Report()
    client = smoke.Client(live_server)
    # This app accepts any Host, so the trusted-host check must fail.
    smoke.http_checks(client, report, expect_trusted_hosts=True)
    smoke.login_checks(client, report, "admin", "wrong-password-123", helper=False)
    assert report.failures == 2
    assert any(line.startswith("FAIL  unknown Host refused") for line in report.lines)
    assert any(line.startswith("FAIL  login") for line in report.lines)


def test_smoke_test_unit_checks(smoke: ModuleType, tmp_path: Path) -> None:
    pids = {"nas-manager.service": "10", "nas-manager-helper.service": "12"}

    def runner(argv: list[str]) -> str:
        unit = argv[-1] if argv[0] == "systemctl" else argv[2]
        if argv[:2] == ["systemctl", "is-active"]:
            return "active"
        if argv[0] == "systemctl":
            return pids[unit]
        return "started"  # journalctl output

    statuses = {
        10: {"Uid": "998\t998\t998\t998", "CapEff": "0000000000000000", "NoNewPrivs": "1"},
        12: {"Uid": "0\t0\t0\t0", "CapEff": "00000000002000c9", "NoNewPrivs": "1"},
    }
    report = smoke.Report()
    smoke.unit_checks(
        report,
        units=["nas-manager.service", "nas-manager-helper.service"],
        socket_path=tmp_path / "missing.sock",
        socket_group="nas-manager",
        runner=runner,
        status_reader=statuses.__getitem__,
    )
    failed = [line for line in report.lines if line.startswith("FAIL")]
    assert failed == [
        f"FAIL  helper socket exists  ([Errno 2] No such file or directory: "
        f"'{tmp_path / 'missing.sock'}')"
    ]
    # A root web process with capabilities is reported.
    statuses[10] = {"Uid": "0\t0\t0\t0", "CapEff": "00000000000000ff", "NoNewPrivs": "0"}
    report = smoke.Report()
    smoke.unit_checks(
        report,
        units=["nas-manager.service"],
        socket_path=tmp_path / "missing.sock",
        socket_group="nas-manager",
        runner=runner,
        status_reader=statuses.__getitem__,
    )
    assert report.failures == 3
