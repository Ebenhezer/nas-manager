#!/usr/bin/env python3
"""Bring /etc/nas-manager/nas-manager.env up to date on install and upgrade.

Standard library only; ``deploy/install.sh`` runs it as root after the env
file exists::

    python3 deploy/env_setup.py --env-file /etc/nas-manager/nas-manager.env \\
        --template deploy/nas-manager.env.example --service-uid 997 --helper

Settings the operator chose are never overwritten. It only:

- appends active template settings the file does not have yet (settings
  added by a newer version), under an "Added by install.sh" comment;
- with ``--helper`` sets ``NAS_HELPER_ENABLED=true`` and adds the service
  account's UID to ``NAS_HELPER_ALLOWED_UIDS``; with ``--no-helper`` sets
  ``NAS_HELPER_ENABLED=false``;
- while ``NAS_TRUSTED_HOSTS`` still holds the template placeholder, asks
  (``--interactive``) for the names the NAS is reached by and whether HTTPS
  is in front, or otherwise leaves it and warns.

The file is rewritten atomically with its owner and mode kept, and only when
something changed. Every change is printed; secret-looking values are not.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

PLACEHOLDER_HOST = "nas.example.org"
# Always trusted: deploy/smoke_test.py and local health checks use them.
LOCAL_HOSTS = ["localhost", "127.0.0.1"]
# Same rule as Settings._host_lists in app/core/config.py.
HOST_RE = re.compile(r"[A-Za-z0-9*.:/\[\]_-]{1,253}")
SECRET_SUFFIXES = ("KEY", "SECRET", "PASSWORD", "TOKEN")
# How the NAS is reached. When an existing file lacks one, the template value
# may differ from the application default it has been running on (secure
# cookies break login over plain HTTP), so it is appended commented out.
TRANSPORT_KEYS = frozenset(
    {
        "NAS_HOST",
        "NAS_PORT",
        "NAS_FORWARDED_ALLOW_IPS",
        "NAS_TRUSTED_HOSTS",
        "NAS_HSTS_MAX_AGE_SECONDS",
        "NAS_SESSION_COOKIE_SECURE",
    }
)
_ACTIVE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

# ask(prompt, default) -> answer; None means non-interactive.
Ask = Callable[[str, str], str]


@dataclass
class Result:
    text: str
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_env(text: str) -> dict[str, str]:
    """Active ``KEY=value`` settings; the last one wins, as in systemd."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ACTIVE_RE.match(line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def set_value(text: str, key: str, value: str) -> str:
    """Rewrite the last active line for ``key`` in place, or append one."""
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        match = _ACTIVE_RE.match(lines[index])
        if match and match.group(1) == key:
            lines[index] = f"{key}={value}"
            return "\n".join(lines) + "\n"
    return append_block(text, "# Added by install.sh.", [(key, value)])


def append_block(text: str, header: str, items: list[tuple[str, str]]) -> str:
    """Append ``items`` after a blank line and a comment header."""
    block = "\n".join([header, *(f"{key}={value}" for key, value in items)])
    return text.rstrip("\n") + "\n\n" + block + "\n"


def add_uid(value: str, uid: int) -> str:
    """Add ``uid`` to a comma-separated UID list unless it is already there."""
    tokens = [token.strip() for token in value.split(",") if token.strip()]
    if str(uid) in tokens:
        return value
    return ",".join([*tokens, str(uid)])


def missing_template_keys(template: str, env: str) -> list[tuple[str, str]]:
    """Active template settings the env file does not set, in template order."""
    present = parse_env(env)
    return [(key, value) for key, value in parse_env(template).items() if key not in present]


def shown(key: str, value: str) -> str:
    return "<hidden>" if key.upper().endswith(SECRET_SUFFIXES) else value


def detect_hosts() -> list[str]:
    """Names and addresses this machine is likely reached by."""
    names = [socket.gethostname(), socket.getfqdn()]
    try:
        out = subprocess.run(
            ["hostname", "-I"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    # Link-local IPv6 addresses are never used in a Host header.
    names += [addr for addr in out.split() if not addr.lower().startswith("fe80:")]
    return _host_list([*names, *LOCAL_HOSTS])


def _host_list(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in items if item.strip()))


def _ask_hosts(ask: Ask, default: list[str]) -> list[str]:
    while True:
        answer = ask("Names/addresses this NAS is reached by (comma-separated)", ",".join(default))
        hosts = _host_list(answer.split(","))
        bad = [host for host in hosts if not HOST_RE.fullmatch(host)]
        if hosts and not bad:
            return _host_list([*hosts, *LOCAL_HOSTS])
        print(f"  invalid: {', '.join(bad) or 'empty list'}", flush=True)


def _ask_yes(ask: Ask, prompt: str) -> bool:
    return ask(f"{prompt} [Y/n]", "y").strip().lower() not in ("n", "no")


def reconcile(
    env: str,
    template: str,
    *,
    service_uid: int,
    helper: bool,
    ask: Ask | None = None,
    hosts: Callable[[], list[str]] = detect_hosts,
    today: datetime.date | None = None,
) -> Result:
    """Return the updated env file text and what changed."""
    result = Result(env)

    def put(key: str, value: str) -> None:
        if parse_env(result.text).get(key) != value:
            result.text = set_value(result.text, key, value)
            result.changes.append(f"set {key}={shown(key, value)}")

    missing = missing_template_keys(template, result.text)
    active = [(key, value) for key, value in missing if key not in TRANSPORT_KEYS]
    review = [(key, value) for key, value in missing if key in TRANSPORT_KEYS]
    day = (today or datetime.date.today()).isoformat()
    if active:
        result.text = append_block(
            result.text, f"# Added by install.sh ({day}): settings new in this version.", active
        )
        result.changes += [f"added {key}={shown(key, value)}" for key, value in active]
    if review:
        result.text = append_block(
            result.text,
            f"# Added by install.sh ({day}): template values, review and uncomment.",
            [(f"# {key}", value) for key, value in review],
        )
        result.changes += [f"added {key} (commented out)" for key, _value in review]
        result.warnings.append(
            f"{', '.join(key for key, _value in review)} not set; the application default "
            "is used. Review the commented template values at the end of the env file."
        )

    put("NAS_HELPER_ENABLED", "true" if helper else "false")
    if helper:
        allowed = parse_env(result.text).get("NAS_HELPER_ALLOWED_UIDS", "")
        updated = add_uid(allowed, service_uid)
        if updated != allowed:
            put("NAS_HELPER_ALLOWED_UIDS", updated)

    trusted = parse_env(result.text).get("NAS_TRUSTED_HOSTS", "")
    if PLACEHOLDER_HOST in trusted.split(","):
        if ask is None:
            result.warnings.append(
                f"NAS_TRUSTED_HOSTS still contains {PLACEHOLDER_HOST}; requests using any "
                "other name are rejected. Set it to the names this NAS is reached by."
            )
        else:
            put("NAS_TRUSTED_HOSTS", ",".join(_ask_hosts(ask, hosts())))
            if not _ask_yes(ask, "Will clients reach NAS Manager over HTTPS (TLS proxy in front)?"):
                # Secure cookies are never sent over plain HTTP: login would fail.
                put("NAS_SESSION_COOKIE_SECURE", "false")
                put("NAS_HSTS_MAX_AGE_SECONDS", "0")
    return result


def write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``, keeping its owner and mode."""
    info = path.stat()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, info.st_mode & 0o7777)
        if os.geteuid() == 0:
            os.chown(tmp, info.st_uid, info.st_gid)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _terminal_ask(prompt: str, default: str) -> str:
    try:
        answer = input(f"{prompt} [{default}]: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--service-uid", type=int, required=True)
    helper = parser.add_mutually_exclusive_group(required=True)
    helper.add_argument("--helper", dest="helper", action="store_true")
    helper.add_argument("--no-helper", dest="helper", action="store_false")
    parser.add_argument("--interactive", action="store_true", help="ask for host names and TLS")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env = args.env_file.read_text(encoding="utf-8")
    result = reconcile(
        env,
        args.template.read_text(encoding="utf-8"),
        service_uid=args.service_uid,
        helper=args.helper,
        ask=_terminal_ask if args.interactive else None,
    )
    if result.text != env:
        write_atomic(args.env_file, result.text)
    for change in result.changes:
        print(f"    {args.env_file}: {change}", flush=True)
    if not result.changes:
        print(f"    {args.env_file}: up to date", flush=True)
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
