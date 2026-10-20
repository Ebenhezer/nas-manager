"""Programmatic Alembic helpers (the CLI uses the same migrations directory)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text

from app.core.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def alembic_config(settings: Settings) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # ConfigParser interpolation treats '%' specially.
    config.set_main_option("sqlalchemy.url", settings.resolved_database_url.replace("%", "%%"))
    return config


def get_head_revision(settings: Settings) -> str | None:
    return ScriptDirectory.from_config(alembic_config(settings)).get_current_head()


def get_current_revision(engine: Engine) -> str | None:
    """Read the applied revision directly (cheap, and silent: no per-call Alembic logging)."""
    with engine.connect() as connection:
        if not inspect(connection).has_table("alembic_version"):
            return None
        rows = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    # More than one row means diverged branches; report it so it never equals a single head.
    return ",".join(sorted(rows)) if rows else None


def upgrade_to_head(settings: Settings) -> None:
    command.upgrade(alembic_config(settings), "head")
