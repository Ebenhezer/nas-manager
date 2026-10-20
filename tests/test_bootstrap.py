from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.core.errors import ConflictError
from app.db.session import Database
from app.models import Role, User
from app.services.bootstrap_service import bootstrap_admin
from app.services.passwords import verify_password

from .conftest import ADMIN_PASSWORD


def test_bootstrap_creates_admin_with_admin_role(db: Database, migrated_settings: Settings) -> None:
    with db.session_factory() as session:
        user = bootstrap_admin(
            session, username="root", password=ADMIN_PASSWORD, settings=migrated_settings
        )
        assert user.username == "root"
        assert user.is_active is True
        assert "admin" in {role.name for role in user.roles}
        assert verify_password(ADMIN_PASSWORD, user.password_hash, migrated_settings)


def test_bootstrap_does_not_leak_plaintext(db: Database, migrated_settings: Settings) -> None:
    with db.session_factory() as session:
        bootstrap_admin(
            session, username="admin", password=ADMIN_PASSWORD, settings=migrated_settings
        )
        row = session.execute(select(User).where(User.username == "admin")).scalar_one()
    assert ADMIN_PASSWORD not in row.password_hash
    assert row.password_hash.startswith("$argon2id$")


def test_bootstrap_fails_when_user_exists_without_reset(
    db: Database, migrated_settings: Settings
) -> None:
    with db.session_factory() as session:
        bootstrap_admin(
            session, username="admin", password=ADMIN_PASSWORD, settings=migrated_settings
        )
        with pytest.raises(ConflictError):
            bootstrap_admin(
                session, username="admin", password=ADMIN_PASSWORD, settings=migrated_settings
            )
        # Role count unchanged
        assert session.execute(select(Role).where(Role.name == "admin")).scalar_one()


def test_bootstrap_reset_updates_password(db: Database, migrated_settings: Settings) -> None:
    with db.session_factory() as session:
        bootstrap_admin(
            session, username="admin", password=ADMIN_PASSWORD, settings=migrated_settings
        )
        refreshed = bootstrap_admin(
            session,
            username="admin",
            password="a-completely-new-password",
            settings=migrated_settings,
            reset_existing=True,
        )
        verified = verify_password(
            "a-completely-new-password", refreshed.password_hash, migrated_settings
        )
        assert verified


def test_cli_bootstrap_admin_end_to_end(migrated_settings: Settings, tmp_path: Path) -> None:
    """Run the real CLI with --password-stdin against a fresh state directory."""
    import os
    import subprocess

    from app.db.migrations import PROJECT_ROOT

    state_dir = tmp_path / "state"
    env = {k: v for k, v in os.environ.items() if not k.startswith("NAS_")} | {
        "NAS_DATA_DIR": str(state_dir),
        "NAS_LOG_TO_CONSOLE": "false",
        "NAS_LOG_TO_FILE": "false",
    }
    python = Path(sys.executable)
    result = subprocess.run(  # noqa: S603
        [
            str(python),
            "-m",
            "app.cli",
            "bootstrap-admin",
            "--username",
            "admin",
            "--password-stdin",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        input=f"{ADMIN_PASSWORD}\n",
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ready" in result.stdout
    # The generated DB must be usable and the admin must log in (no plaintext leak).
    db_engine = Path(state_dir) / "nas.db"
    assert db_engine.is_file()
    from app.db.session import Database

    instance = Database(f"sqlite:///{db_engine}")
    try:
        with instance.session_factory() as session:
            user = session.execute(select(User).where(User.username == "admin")).scalar_one()
            assert verify_password(ADMIN_PASSWORD, user.password_hash, migrated_settings)
            assert "admin" in {r.name for r in user.roles}
    finally:
        instance.dispose()
