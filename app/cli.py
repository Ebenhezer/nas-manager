"""Command-line utilities.

``python -m app.cli bootstrap-admin`` creates (or resets) the initial
administrator. The password is read interactively or from the first line of
stdin (``--password-stdin``); it is never taken from argv, an environment
variable, or echoed or logged.

Phase 11 adds two recovery commands that must run with the services
stopped:

* ``restore-system --run <id>`` installs a system-state snapshot that a
  restore run staged (database, credentials key). The current files are
  renamed to ``*.before-restore-<timestamp>``, never deleted.
* ``backup-recover --repo-dir <dir>`` reads a backup repository directly
  (for example after the database itself was lost): it lists the snapshots,
  or restores one into an empty directory. The repository passphrase is
  read like the admin password.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.errors import AppError, ConflictError
from app.db.migrations import upgrade_to_head
from app.db.session import Database
from app.services import audit
from app.services.backup import engine, sources
from app.services.backup.repository import Repository
from app.services.bootstrap_service import bootstrap_admin
from app.services.remote.local import LocalProvider


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
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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

    restore = sub.add_parser(
        "restore-system",
        help="install a staged system-state restore (stop nas-manager and the worker first)",
    )
    restore.add_argument("--run", type=int, required=True, help="id of the staged restore run")
    restore.add_argument(
        "--yes",
        action="store_true",
        help="confirm that nas-manager and nas-manager-worker are stopped",
    )

    recover = sub.add_parser(
        "backup-recover",
        help="list or restore snapshots straight from a repository directory",
    )
    recover.add_argument(
        "--repo-dir", type=Path, required=True, help="the repository directory (holds repo.json)"
    )
    recover.add_argument("--snapshot", help="snapshot id to restore (omit to list snapshots)")
    recover.add_argument("--target", type=Path, help="empty or new directory to restore into")
    recover.add_argument(
        "--path", action="append", default=[], help="restore only this file or folder (repeat)"
    )
    recover.add_argument(
        "--passphrase-stdin",
        action="store_true",
        help="read the repository passphrase from the first line of stdin",
    )
    return parser


def _bootstrap(args: argparse.Namespace, settings: Settings) -> int:
    settings.ensure_directories()
    # The systemd unit already migrates in ExecStartPre; for manual installs
    # this makes the CLI work standalone and stay idempotent.
    upgrade_to_head(settings)

    username = (args.username or settings.admin_username).strip().lower()
    password = _read_password(f"New password for '{username}': ", args.password_stdin)

    db = Database(settings.resolved_database_url)
    try:
        with db.session_factory() as session:
            user = bootstrap_admin(
                session,
                username=username,
                password=password,
                settings=settings,
                reset_existing=bool(args.reset_password),
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _install(staged: Path, destination: Path, mode: int, stamp: str) -> Path | None:
    """Copy ``staged`` over ``destination``; keep the old file as *.before-restore-*."""
    kept = None
    if destination.exists():
        kept = destination.with_name(f"{destination.name}.before-restore-{stamp}")
        os.rename(destination, kept)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.restoring")
    shutil.copyfile(staged, temporary)
    os.chmod(temporary, mode)
    os.replace(temporary, destination)
    return kept


def _restore_system(args: argparse.Namespace, settings: Settings) -> int:
    staged = engine.staging_restore_dir(settings, args.run)
    marker_path = staged / engine.RESTORE_MARKER
    if not marker_path.is_file():
        print(f"error: no staged restore at {staged}", file=sys.stderr)
        return 2
    marker = json.loads(marker_path.read_text())
    expected: dict[str, str] = dict(marker.get("files") or {})
    if sources.SYSTEM_DATABASE not in expected:
        print("error: the staged snapshot contains no database", file=sys.stderr)
        return 2
    for name, digest in expected.items():
        if _sha256(staged / name) != digest:
            print(f"error: staged file {name} does not match its checksum", file=sys.stderr)
            return 2
    if not args.yes:
        print(
            "Refusing to continue without --yes. Stop the services first:\n"
            "  sudo systemctl stop nas-manager nas-manager-worker\n"
            f"then re-run with --yes to install snapshot {marker.get('snapshot_id')}.",
            file=sys.stderr,
        )
        return 2

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    database = sources.sqlite_path(settings)
    kept: list[str] = []
    for suffix in ("-wal", "-shm"):
        sidecar = database.with_name(database.name + suffix)
        if sidecar.exists():
            target = sidecar.with_name(f"{sidecar.name}.before-restore-{stamp}")
            os.rename(sidecar, target)
            kept.append(str(target))
    previous = _install(staged / sources.SYSTEM_DATABASE, database, 0o640, stamp)
    if previous is not None:
        kept.append(str(previous))
    if sources.SYSTEM_KEY in expected:
        key = settings.resolved_credentials_key_file
        if not key.exists() or _sha256(key) != expected[sources.SYSTEM_KEY]:
            previous = _install(staged / sources.SYSTEM_KEY, key, 0o600, stamp)
            if previous is not None:
                kept.append(str(previous))

    upgrade_to_head(settings)
    db = Database(settings.resolved_database_url)
    try:
        with db.session_factory() as session:
            audit.record_audit_event(
                session,
                event=audit.BACKUP_SYSTEM_RESTORED,
                category=audit.CATEGORY_BACKUP,
                actor_username="cli",
                target_type="backup_snapshot",
                target_id=str(marker.get("snapshot_id")),
                details={"run_id": args.run, "kept": kept},
            )
    finally:
        db.dispose()
    print(f"Installed system snapshot {marker.get('snapshot_id')}.")
    for path in kept:
        print(f"  previous file kept as {path}")
    if sources.SYSTEM_SAMBA in expected:
        print(
            f"The snapshot's Samba configuration is at {staged / sources.SYSTEM_SAMBA}.\n"
            "Shares come back with the database: open Samba in the web interface and apply "
            "the configuration to regenerate smb.conf."
        )
    print("Start the services: sudo systemctl start nas-manager nas-manager-worker")
    return 0


def _recover(args: argparse.Namespace, settings: Settings) -> int:
    repo_dir: Path = args.repo_dir.expanduser().resolve()
    if not (repo_dir / "repo.json").is_file():
        print(f"error: {repo_dir} is not a backup repository (no repo.json)", file=sys.stderr)
        return 2
    passphrase = _read_password("Repository passphrase: ", args.passphrase_stdin)
    provider = LocalProvider(repo_dir.parent)
    with provider:
        repo = Repository.open(
            provider,
            repo_dir.name,
            passphrase,
            staging=settings.resolved_backup_staging_dir / "tmp",
        )
        if not args.snapshot:
            for snapshot_id in repo.list_snapshot_ids():
                try:
                    manifest = repo.read_snapshot(snapshot_id)
                except AppError as exc:
                    print(f"{snapshot_id}  (unreadable: {exc.message})")
                    continue
                job = manifest.get("job") or {}
                source = manifest.get("source") or {}
                print(
                    f"{snapshot_id}  job={job.get('name')}  source={source.get('label')}  "
                    f"files={len(manifest['entries'])}"
                )
            return 0
        if args.target is None:
            print("error: --target is required with --snapshot", file=sys.stderr)
            return 2
        target: Path = args.target.expanduser().resolve()
        if target.exists() and any(target.iterdir()):
            print(f"error: {target} is not empty", file=sys.stderr)
            return 2
        manifest = repo.read_snapshot(args.snapshot)
        entries = engine.select_entries(manifest, list(args.path))
        restored, problems = engine.restore_to_directory(repo, entries, target)
    print(f"Restored {restored} of {len(entries)} file(s) into {target}.")
    for problem in problems:
        print(f"  {problem['path']}: {problem['error']}", file=sys.stderr)
    return 1 if problems else 0


def main() -> int:
    args = _build_parser().parse_args()
    settings = get_settings()
    try:
        if args.command == "restore-system":
            return _restore_system(args, settings)
        if args.command == "backup-recover":
            return _recover(args, settings)
    except AppError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 2
    return _bootstrap(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
