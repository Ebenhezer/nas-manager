"""Remote connections and sync jobs, baselines, runs and conflicts (Phase 7).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOKENS = ["remotes:read", "remotes:write"]


def _grant_permissions() -> None:
    """Grant the Phase 7 permission tokens to the seeded system roles.

    Kept as raw statements so the migration never imports application code.
    ``admin`` receives read+write, ``operator`` read-only (connection and job
    metadata and run history; stored credentials are never readable).
    """
    bind = op.get_bind()
    roles = sa.table("roles", sa.column("id", sa.Integer), sa.column("name", sa.String))
    grants = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Integer),
        sa.column("permission", sa.String),
    )
    for role_name, tokens in (
        ("admin", _TOKENS),
        ("operator", ["remotes:read"]),
    ):
        role_id = bind.execute(
            sa.select(roles.c.id).where(roles.c.name == role_name)
        ).scalar_one_or_none()
        if role_id is None:
            continue
        for token in tokens:
            exists = bind.execute(
                sa.select(grants.c.permission).where(
                    grants.c.role_id == role_id, grants.c.permission == token
                )
            ).first()
            if exists is None:
                bind.execute(grants.insert(), {"role_id": role_id, "permission": token})


def _revoke_permissions() -> None:
    """Remove the Phase 7 tokens from the seeded system roles (downgrade)."""
    bind = op.get_bind()
    roles = sa.table("roles", sa.column("id", sa.Integer), sa.column("name", sa.String))
    grants = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Integer),
        sa.column("permission", sa.String),
    )
    role_ids = [
        row[0]
        for row in bind.execute(
            sa.select(roles.c.id).where(roles.c.name.in_(["admin", "operator"]))
        ).all()
    ]
    if role_ids:
        bind.execute(
            grants.delete().where(
                grants.c.role_id.in_(role_ids),
                grants.c.permission.in_(_TOKENS),
            )
        )


def upgrade() -> None:
    op.create_table(
        "remote_connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("secrets_encrypted", sa.Text(), nullable=True),
        sa.Column("oauth_pending_encrypted", sa.Text(), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_remote_connections_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_remote_connections")),
        sa.UniqueConstraint("name", name=op.f("uq_remote_connections_name")),
    )
    op.create_table(
        "sync_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("local_root", sa.String(length=1024), nullable=False),
        sa.Column("local_path", sa.Text(), server_default="", nullable=False),
        sa.Column("remote_path", sa.Text(), server_default="", nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column(
            "conflict_policy", sa.String(length=16), server_default="keep_both", nullable=False
        ),
        sa.Column("delete_policy", sa.String(length=16), server_default="ignore", nullable=False),
        sa.Column("exclude_patterns", sa.Text(), server_default="[]", nullable=False),
        sa.Column("schedule_minutes", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=16), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["remote_connections.id"],
            name=op.f("fk_sync_jobs_connection_id_remote_connections"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_sync_jobs_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_jobs")),
        sa.UniqueConstraint("name", name=op.f("uq_sync_jobs_name")),
    )
    with op.batch_alter_table("sync_jobs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_sync_jobs_connection_id"), ["connection_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_sync_jobs_next_run_at"), ["next_run_at"], unique=False)

    op.create_table(
        "sync_job_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("local_size", sa.BigInteger(), nullable=False),
        sa.Column("local_mtime_ns", sa.BigInteger(), nullable=False),
        sa.Column("remote_size", sa.BigInteger(), nullable=False),
        sa.Column("remote_mtime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remote_etag", sa.String(length=256), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["sync_jobs.id"],
            name=op.f("fk_sync_job_entries_job_id_sync_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_job_entries")),
        sa.UniqueConstraint("job_id", "path", name="uq_sync_job_entries_job_id_path"),
    )
    with op.batch_alter_table("sync_job_entries", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sync_job_entries_job_id"), ["job_id"], unique=False)

    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("dry_run", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("requested_by", sa.Integer(), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("uploaded", sa.Integer(), server_default="0", nullable=False),
        sa.Column("downloaded", sa.Integer(), server_default="0", nullable=False),
        sa.Column("conflicts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("archived", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("errors", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bytes_transferred", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), server_default="{}", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["sync_jobs.id"],
            name=op.f("fk_sync_runs_job_id_sync_jobs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_sync_runs_requested_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_runs")),
    )
    with op.batch_alter_table("sync_runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sync_runs_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_sync_runs_job_id"), ["job_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_sync_runs_status"), ["status"], unique=False)

    op.create_table(
        "sync_conflicts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("resolution", sa.String(length=32), nullable=False),
        sa.Column("conflict_copy_path", sa.Text(), nullable=True),
        sa.Column("acknowledged", sa.Boolean(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["sync_jobs.id"],
            name=op.f("fk_sync_conflicts_job_id_sync_jobs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["sync_runs.id"],
            name=op.f("fk_sync_conflicts_run_id_sync_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_conflicts")),
    )
    with op.batch_alter_table("sync_conflicts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_sync_conflicts_job_id"), ["job_id"], unique=False)

    _grant_permissions()


def downgrade() -> None:
    _revoke_permissions()
    with op.batch_alter_table("sync_conflicts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sync_conflicts_job_id"))

    op.drop_table("sync_conflicts")
    with op.batch_alter_table("sync_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sync_runs_status"))
        batch_op.drop_index(batch_op.f("ix_sync_runs_job_id"))
        batch_op.drop_index(batch_op.f("ix_sync_runs_created_at"))

    op.drop_table("sync_runs")
    with op.batch_alter_table("sync_job_entries", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sync_job_entries_job_id"))

    op.drop_table("sync_job_entries")
    with op.batch_alter_table("sync_jobs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sync_jobs_next_run_at"))
        batch_op.drop_index(batch_op.f("ix_sync_jobs_connection_id"))

    op.drop_table("sync_jobs")
    op.drop_table("remote_connections")
