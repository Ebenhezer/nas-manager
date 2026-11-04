"""Phase 14: deployment artifacts (systemd units, scripts, smoke test)."""

from __future__ import annotations

import datetime
import importlib.util
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
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


def test_installer_installs_the_helper_and_reconciles_the_env_file() -> None:
    text = (DEPLOY / "install.sh").read_text()
    assert "\nWITH_HELPER=true\n" in text
    assert '"$PYTHON" "$SOURCE_DIR/deploy/env_setup.py" "${ENV_ARGS[@]}"' in text
    # The configuration is validated before any unit is (re)started.
    assert text.index("Settings(_env_file=") < text.index("# 6. Start")


def test_deploy_files_have_lf_line_endings() -> None:
    # read_text() hides CRLF; a CRLF shebang makes the scripts unrunnable.
    for path in DEPLOY.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            assert b"\r" not in path.read_bytes(), path


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


# --- deploy/env_setup.py ----------------------------------------------------

TEMPLATE = (DEPLOY / "nas-manager.env.example").read_text()


@pytest.fixture(scope="module")
def env_setup() -> ModuleType:
    spec = importlib.util.spec_from_file_location("env_setup", DEPLOY / "env_setup.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["env_setup"] = module
    spec.loader.exec_module(module)
    return module


def _answers(*replies: str) -> Callable[[str, str], str]:
    pending = list(replies)
    return lambda _prompt, default: pending.pop(0) or default


def test_env_setup_parse_and_set(env_setup: ModuleType) -> None:
    text = "# NAS_A=commented\nNAS_A=1\nNAS_B=x\nNAS_A=2\n"
    assert env_setup.parse_env(text) == {"NAS_A": "2", "NAS_B": "x"}
    # The last active line is rewritten in place; comments stay.
    assert (
        env_setup.set_value(text, "NAS_A", "3") == "# NAS_A=commented\nNAS_A=1\nNAS_B=x\nNAS_A=3\n"
    )
    appended = env_setup.set_value(text, "NAS_C", "y")
    assert appended.startswith(text) and appended.endswith("\nNAS_C=y\n")


def test_env_setup_add_uid(env_setup: ModuleType) -> None:
    assert env_setup.add_uid("0", 997) == "0,997"
    assert env_setup.add_uid("", 997) == "997"
    assert env_setup.add_uid("0, 997", 997) == "0, 997"
    assert env_setup.add_uid("9970", 997) == "9970,997"


def test_env_setup_fresh_template_with_helper(env_setup: ModuleType) -> None:
    result = env_setup.reconcile(TEMPLATE, TEMPLATE, service_uid=997, helper=True)
    values = env_setup.parse_env(result.text)
    assert values["NAS_HELPER_ENABLED"] == "true"
    assert values["NAS_HELPER_ALLOWED_UIDS"] == "997"
    assert result.changes == ["set NAS_HELPER_ALLOWED_UIDS=997"]
    # Non-interactive: the placeholder is kept and reported.
    assert "nas.example.org" in values["NAS_TRUSTED_HOSTS"]
    assert len(result.warnings) == 1


def test_env_setup_without_helper(env_setup: ModuleType) -> None:
    result = env_setup.reconcile(TEMPLATE, TEMPLATE, service_uid=997, helper=False)
    values = env_setup.parse_env(result.text)
    assert values["NAS_HELPER_ENABLED"] == "false"
    assert "NAS_HELPER_ALLOWED_UIDS" not in values


def test_env_setup_repairs_an_install_made_without_the_helper(env_setup: ModuleType) -> None:
    env = env_setup.set_value(TEMPLATE, "NAS_HELPER_ENABLED", "false")
    env = env_setup.set_value(env, "NAS_TRUSTED_HOSTS", "pi-storage,localhost")
    result = env_setup.reconcile(env, TEMPLATE, service_uid=997, helper=True)
    values = env_setup.parse_env(result.text)
    assert values["NAS_HELPER_ENABLED"] == "true"
    assert values["NAS_HELPER_ALLOWED_UIDS"] == "997"
    assert values["NAS_TRUSTED_HOSTS"] == "pi-storage,localhost"
    assert result.warnings == []
    # A second run changes nothing.
    again = env_setup.reconcile(result.text, TEMPLATE, service_uid=997, helper=True)
    assert again.text == result.text and again.changes == []


def test_env_setup_appends_missing_template_settings(env_setup: ModuleType) -> None:
    env = "NAS_HOST=192.168.1.20\nNAS_TRUSTED_HOSTS=nas.lan\nNAS_HELPER_ENABLED=true\n"
    result = env_setup.reconcile(
        env, TEMPLATE, service_uid=997, helper=True, today=datetime.date(2026, 11, 4)
    )
    values = env_setup.parse_env(result.text)
    assert result.text.startswith(env)
    assert "# Added by install.sh (2026-11-04)" in result.text
    # Operator values are kept; the template only fills the gaps.
    assert values["NAS_HOST"] == "192.168.1.20"
    assert values["NAS_TRUSTED_HOSTS"] == "nas.lan"
    assert values["NAS_LOG_LEVEL"] == "INFO"
    # Transport settings would change how a running install is reached: they
    # are only proposed (commented out) and reported.
    assert "NAS_SESSION_COOKIE_SECURE" not in values
    assert "\n# NAS_SESSION_COOKIE_SECURE=true\n" in result.text
    assert "NAS_SESSION_COOKIE_SECURE" in result.warnings[0]
    template_keys = set(env_setup.parse_env(TEMPLATE)) - env_setup.TRANSPORT_KEYS
    assert template_keys <= set(values)


def test_env_setup_asks_for_hosts_and_tls(env_setup: ModuleType) -> None:
    result = env_setup.reconcile(
        TEMPLATE,
        TEMPLATE,
        service_uid=997,
        helper=True,
        ask=_answers("pi-storage, bad host!", "pi-storage,192.168.1.20", "n"),
        hosts=lambda: ["pi-storage", "localhost", "127.0.0.1"],
    )
    values = env_setup.parse_env(result.text)
    assert values["NAS_TRUSTED_HOSTS"] == "pi-storage,192.168.1.20,localhost,127.0.0.1"
    assert values["NAS_SESSION_COOKIE_SECURE"] == "false"
    assert values["NAS_HSTS_MAX_AGE_SECONDS"] == "0"
    assert result.warnings == []


def test_env_setup_keeps_tls_by_default(env_setup: ModuleType) -> None:
    result = env_setup.reconcile(
        TEMPLATE,
        TEMPLATE,
        service_uid=997,
        helper=True,
        ask=_answers("", ""),
        hosts=lambda: ["pi-storage", "localhost", "127.0.0.1"],
    )
    values = env_setup.parse_env(result.text)
    assert values["NAS_TRUSTED_HOSTS"] == "pi-storage,localhost,127.0.0.1"
    assert values["NAS_SESSION_COOKIE_SECURE"] == "true"


def test_env_setup_main_rewrites_in_place(env_setup: ModuleType, tmp_path: Path) -> None:
    env_file = tmp_path / "nas-manager.env"
    env_file.write_text("NAS_HELPER_ENABLED=false\nNAS_TRUSTED_HOSTS=nas.lan\n")
    env_file.chmod(0o640)
    template = tmp_path / "template"
    template.write_text("NAS_HELPER_ENABLED=true\n")
    args = ["--env-file", str(env_file), "--template", str(template), "--service-uid", "997"]
    assert env_setup.main([*args, "--helper"]) == 0
    assert env_setup.parse_env(env_file.read_text())["NAS_HELPER_ALLOWED_UIDS"] == "997"
    assert env_file.stat().st_mode & 0o777 == 0o640
    # No temporary file is left behind.
    assert sorted(tmp_path.iterdir()) == sorted([env_file, template])


def test_env_setup_hides_secrets(env_setup: ModuleType) -> None:
    assert env_setup.shown("NAS_SMTP_PASSWORD", "hunter2") == "<hidden>"
    assert env_setup.shown("NAS_HOST", "127.0.0.1") == "127.0.0.1"
