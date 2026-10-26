from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect

from app.core.config import Settings
from app.db.migrations import (
    alembic_config,
    get_current_revision,
    get_head_revision,
    upgrade_to_head,
)
from app.db.session import Database


def test_upgrade_creates_schema_and_reaches_head(settings: Settings) -> None:
    settings.ensure_directories()
    db = Database(settings.resolved_database_url)
    try:
        assert get_current_revision(db.engine) is None
        upgrade_to_head(settings)
        assert get_current_revision(db.engine) == get_head_revision(settings)
        tables = set(inspect(db.engine).get_table_names())
        assert {
            "app_settings",
            "alembic_version",
            "users",
            "groups",
            "roles",
            "role_permissions",
            "auth_sessions",
            "audit_events",
            "user_groups",
            "user_roles",
            "group_roles",
            "samba_shares",
            "public_upload_quotas",
            "public_upload_files",
        } <= tables
    finally:
        db.dispose()


def test_upgrade_is_idempotent(settings: Settings) -> None:
    settings.ensure_directories()
    upgrade_to_head(settings)
    upgrade_to_head(settings)
    db = Database(settings.resolved_database_url)
    try:
        assert get_current_revision(db.engine) == get_head_revision(settings)
    finally:
        db.dispose()


def test_downgrade_removes_schema(settings: Settings) -> None:
    settings.ensure_directories()
    upgrade_to_head(settings)
    command.downgrade(alembic_config(settings), "base")
    db = Database(settings.resolved_database_url)
    try:
        assert "app_settings" not in inspect(db.engine).get_table_names()
        assert get_current_revision(db.engine) is None
    finally:
        db.dispose()


def test_single_migration_head(settings: Settings) -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config(settings))
    assert len(script.get_heads()) == 1


def test_models_match_migrations(migrated_settings: Settings) -> None:
    """Autogenerate must find no drift between ORM models and migrations."""
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    import app.models  # noqa: F401
    from app.db.base import Base

    db = Database(migrated_settings.resolved_database_url)
    try:
        with db.engine.connect() as connection:
            context = MigrationContext.configure(connection)
            assert compare_metadata(context, Base.metadata) == []
    finally:
        db.dispose()


def test_current_revision_reports_diverged_rows(migrated_settings: Settings) -> None:
    from sqlalchemy import text

    db = Database(migrated_settings.resolved_database_url)
    try:
        with db.engine.begin() as connection:
            connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('zzz')"))
        current = get_current_revision(db.engine)
        assert current == "0004,zzz"
        assert current != get_head_revision(migrated_settings)
    finally:
        db.dispose()


def test_alembic_cli_runs_from_project_root_and_creates_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the console script must import `app` and work before the app ever started."""
    import subprocess
    import sys

    from app.db.migrations import PROJECT_ROOT

    state_dir = tmp_path / "fresh" / "state"  # does not exist yet
    env = {k: v for k, v in os.environ.items() if not k.startswith("NAS_")} | {
        "NAS_DATA_DIR": str(state_dir)
    }
    alembic_bin = Path(sys.executable).parent / "alembic"
    result = subprocess.run(  # noqa: S603
        [str(alembic_bin), "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (state_dir / "nas.db").is_file()
    current = subprocess.run(  # noqa: S603
        [str(alembic_bin), "current"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert "0004" in current.stdout + current.stderr
