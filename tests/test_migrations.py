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
            "api_tokens",
            "upload_sessions",
            "file_changes",
            *PHASE10_TABLES,
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
        assert current == "0010,zzz"
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
    assert "0010" in current.stdout + current.stderr


def test_phase7_downgrade_and_reupgrade(settings: Settings) -> None:
    """0007 drops the remote-sync tables and grants on downgrade and re-applies cleanly."""
    from sqlalchemy import text

    settings.ensure_directories()
    upgrade_to_head(settings)
    tables = {"remote_connections", "sync_jobs", "sync_job_entries", "sync_runs", "sync_conflicts"}
    grants_sql = text(
        "SELECT r.name, p.permission FROM role_permissions p JOIN roles r ON r.id = p.role_id"
        " WHERE p.permission LIKE 'remotes:%' ORDER BY 1, 2"
    )
    db = Database(settings.resolved_database_url)
    try:
        assert tables <= set(inspect(db.engine).get_table_names())
        with db.engine.connect() as connection:
            assert connection.execute(grants_sql).all() == [
                ("admin", "remotes:read"),
                ("admin", "remotes:write"),
                ("operator", "remotes:read"),
            ]
        command.downgrade(alembic_config(settings), "0006")
        assert not tables & set(inspect(db.engine).get_table_names())
        with db.engine.connect() as connection:
            assert connection.execute(grants_sql).all() == []
        upgrade_to_head(settings)
        assert get_current_revision(db.engine) == get_head_revision(settings)
    finally:
        db.dispose()


def test_phase8_downgrade_and_reupgrade(settings: Settings) -> None:
    """0008 drops the media tables and every media:* grant, and re-applies cleanly."""
    from sqlalchemy import text

    settings.ensure_directories()
    upgrade_to_head(settings)
    tables = {
        "media_libraries",
        "media_library_groups",
        "media_items",
        "media_albums",
        "media_album_items",
        "media_changes",
    }
    grants_sql = text(
        "SELECT r.name, p.permission FROM role_permissions p JOIN roles r ON r.id = p.role_id"
        " WHERE p.permission LIKE 'media:%' ORDER BY 1, 2"
    )
    db = Database(settings.resolved_database_url)
    try:
        assert tables <= set(inspect(db.engine).get_table_names())
        with db.engine.connect() as connection:
            assert connection.execute(grants_sql).all() == [
                ("admin", "media:admin"),
                ("admin", "media:read"),
                ("admin", "media:write"),
                ("operator", "media:read"),
                ("operator", "media:write"),
                ("viewer", "media:read"),
                ("viewer", "media:write"),
            ]
        command.downgrade(alembic_config(settings), "0007")
        assert not tables & set(inspect(db.engine).get_table_names())
        with db.engine.connect() as connection:
            assert connection.execute(grants_sql).all() == []
        upgrade_to_head(settings)
        assert get_current_revision(db.engine) == get_head_revision(settings)
    finally:
        db.dispose()


def test_phase9_downgrade_and_reupgrade(settings: Settings) -> None:
    """0009 drops media_transcodes and the playback/rotation columns, and re-applies."""
    settings.ensure_directories()
    upgrade_to_head(settings)
    db = Database(settings.resolved_database_url)
    try:
        assert "media_transcodes" in inspect(db.engine).get_table_names()
        columns = {c["name"] for c in inspect(db.engine).get_columns("media_items")}
        assert {"playback", "rotation"} <= columns
        command.downgrade(alembic_config(settings), "0008")
        assert "media_transcodes" not in inspect(db.engine).get_table_names()
        columns = {c["name"] for c in inspect(db.engine).get_columns("media_items")}
        assert not {"playback", "rotation"} & columns
        upgrade_to_head(settings)
        assert get_current_revision(db.engine) == get_head_revision(settings)
    finally:
        db.dispose()


PHASE10_TABLES = {
    "metric_samples",
    "monitor_checks",
    "alert_rules",
    "alerts",
    "alert_channels",
    "alert_notifications",
}


def test_phase10_downgrade_and_reupgrade(settings: Settings) -> None:
    """0010 seeds the built-in rules, drops its six tables, and re-applies."""
    from sqlalchemy import text

    settings.ensure_directories()
    upgrade_to_head(settings)
    db = Database(settings.resolved_database_url)
    try:
        assert set(inspect(db.engine).get_table_names()) >= PHASE10_TABLES
        with db.engine.connect() as connection:
            rules = connection.execute(
                text("SELECT name, metric, builtin, enabled FROM alert_rules")
            ).all()
        assert len(rules) == 10
        assert all(row.builtin and row.enabled for row in rules)
        assert {row.metric for row in rules} >= {"disk_percent", "raid_degraded", "service_down"}
        command.downgrade(alembic_config(settings), "0009")
        assert not PHASE10_TABLES & set(inspect(db.engine).get_table_names())
        upgrade_to_head(settings)
        assert get_current_revision(db.engine) == "0010"
        with db.engine.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM alert_rules")).scalar_one()
        assert count == 10
    finally:
        db.dispose()
