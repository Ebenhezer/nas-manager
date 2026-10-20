"""Command-line utilities.

``python -m app.cli bootstrap-admin`` creates (or resets) the initial
administrator. The password is read interactively or from the first line of
stdin (``--password-stdin``); it is never taken from argv, an environment
variable, or echoed or logged.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from app.core.config import Settings, get_settings
from app.core.errors import ConflictError
from app.db.migrations import upgrade_to_head
from app.db.session import Database
from app.services.bootstrap_service import bootstrap_admin


def _read_password(prompt: str, password_stdin: bool) -> str:
    if password_stdin:
        password = sys.stdin.readline()
        if not password:
            raise SystemExit("error: stdin closed before a password line was read")
        password = password.rstrip("\r\n")
        if not password:
            raise SystemExit("error: empty password on stdin")
        return password
    password = getpass.getpass(prompt)
    if not password:
        raise SystemExit("error: empty password")
    return password


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap = sub.add_parser(
        "bootstrap-admin",
        help="create the initial administrator (or reset its password with --reset-password)",
    )
    bootstrap.add_argument(
        "--username",
        default=None,
        help=f"administrator username (default: {Settings().admin_username!r})",
    )
    bootstrap.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from the first line of stdin instead of a prompt",
    )
    bootstrap.add_argument(
        "--reset-password",
        action="store_true",
        help="reset the password of an existing user and ensure it holds the admin role",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    settings = get_settings()
    settings.ensure_directories()
    # The systemd unit already migrates in ExecStartPre; for manual installs
    # this makes the CLI work standalone and stay idempotent.
    upgrade_to_head(settings)

    username = (args.username or settings.admin_username).strip().lower()
    password = _read_password(
        f"New password for '{username}': ", getattr(args, "password_stdin", False)
    )

    db = Database(settings.resolved_database_url)
    try:
        with db.session_factory() as session:
            user = bootstrap_admin(
                session,
                username=username,
                password=password,
                settings=settings,
                reset_existing=bool(getattr(args, "reset_password", False)),
            )
            print(
                f"Administrator '{user.username}' is ready. Log in at "
                f"http://{settings.host}:{settings.port}/\n"
                "Delete this command's terminal history if the password was typed."
            )
            return 0
    except ConflictError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        db.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
