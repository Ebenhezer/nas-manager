"""Backup repositories, jobs, runs, snapshot catalogue and blob cache (Phase 11).

Adds the ``backup:read`` (admin, operator), ``backup:write`` and
``backup:restore`` (admin) permission tokens.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TOKENS = ["backup:read", "backup:write", "backup:restore"]


def _roles_and_grants() -> tuple[sa.TableClause, sa.TableClause]:
    roles = sa.table("roles", sa.column("id", sa.Integer), sa.column("name", sa.String))
    grants = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Integer),
        sa.column("permission", sa.String),
    )
    return roles, grants


def _grant_permissions() -> None:
    """Grant the Phase 11 tokens to the seeded system roles (raw statements,
    so the migration never imports application code)."""
    bind = op.get_bind()
    roles, grants = _roles_and_grants()
    for role_name, tokens in (("admin", _TOKENS), ("operator", ["backup:read"])):
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
    bind = op.get_bind()
    roles, grants = _roles_and_grants()
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


def _timestamps() -> list[sa.Column[sa.DateTime]]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "backup_repositories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("target_kind", sa.String(length=16), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=True),
        sa.Column("local_root", sa.String(length=1024), nullable=True),
        sa.Column("repo_path", sa.Text(), server_default="", nullable=False),
        sa.Column("passphrase_encrypted", sa.Text(), nullable=False),
        sa.Column("initialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verify_status", sa.String(length=16), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["remote_connections.id"],
            name=op.f("fk_backup_repositories_connection_id_remote_connections"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_backup_repositories_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backup_repositories")),
        sa.UniqueConstraint("name", name=op.f("uq_backup_repositories_name")),
    )
    with op.batch_alter_table("backup_repositories", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_backup_repositories_connection_id"), ["connection_id"], unique=False
        )

    op.create_table(
        "backup_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("source_root", sa.String(length=1024), nullable=True),
        sa.Column("source_path", sa.Text(), server_default="", nullable=False),
        sa.Column("source_connection_id", sa.Integer(), nullable=True),
        sa.Column("exclude_patterns", sa.Text(), server_default="[]", nullable=False),
        sa.Column("schedule_minutes", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("keep_last", sa.Integer(), server_default="7", nullable=False),
        sa.Column("keep_daily", sa.Integer(), server_default="7", nullable=False),
        sa.Column("keep_weekly", sa.Integer(), server_default="4", nullable=False),
        sa.Column("keep_monthly", sa.Integer(), server_default="6", nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(length=16), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_backup_jobs_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["backup_repositories.id"],
            name=op.f("fk_backup_jobs_repository_id_backup_repositories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_connection_id"],
            ["remote_connections.id"],
            name=op.f("fk_backup_jobs_source_connection_id_remote_connections"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backup_jobs")),
        sa.UniqueConstraint("name", name=op.f("uq_backup_jobs_name")),
    )
    with op.batch_alter_table("backup_jobs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_backup_jobs_next_run_at"), ["next_run_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_backup_jobs_repository_id"), ["repository_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_backup_jobs_source_connection_id"),
            ["source_connection_id"],
            unique=False,
        )

    op.create_table(
        "backup_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("dry_run", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("requested_by", sa.Integer(), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("files", sa.Integer(), server_default="0", nullable=False),
        sa.Column("new_blobs", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reused", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped", sa.Integer(), server_default="0", nullable=False),
        sa.Column("errors", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bytes_processed", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("bytes_uploaded", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=True),
        sa.Column("params", sa.Text(), server_default="{}", nullable=False),
        sa.Column("summary", sa.Text(), server_default="{}", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
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
            ["backup_jobs.id"],
            name=op.f("fk_backup_runs_job_id_backup_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["backup_repositories.id"],
            name=op.f("fk_backup_runs_repository_id_backup_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_backup_runs_requested_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backup_runs")),
    )
    with op.batch_alter_table("backup_runs", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_backup_runs_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_backup_runs_job_id"), ["job_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_backup_runs_repository_id"), ["repository_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_backup_runs_status"), ["status"], unique=False)

    op.create_table(
        "backup_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("job_name", sa.String(length=64), nullable=True),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("source_label", sa.Text(), server_default="", nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("file_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verify_status", sa.String(length=16), nullable=True),
        sa.Column("verify_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["backup_jobs.id"],
            name=op.f("fk_backup_snapshots_job_id_backup_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["backup_repositories.id"],
            name=op.f("fk_backup_snapshots_repository_id_backup_repositories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_backup_snapshots")),
        sa.UniqueConstraint(
            "repository_id", "snapshot_id", name="uq_backup_snapshots_repository_id_snapshot_id"
        ),
    )
    with op.batch_alter_table("backup_snapshots", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_backup_snapshots_job_id"), ["job_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_backup_snapshots_repository_id"), ["repository_id"], unique=False
        )

    op.create_table(
        "backup_blobs",
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("blob", sa.String(length=64), nullable=False),
        sa.Column("size", sa.BigInteger(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["backup_repositories.id"],
            name=op.f("fk_backup_blobs_repository_id_backup_repositories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("repository_id", "blob", name=op.f("pk_backup_blobs")),
    )

    _grant_permissions()


def downgrade() -> None:
    _revoke_permissions()
    op.drop_table("backup_blobs")
    with op.batch_alter_table("backup_snapshots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_backup_snapshots_repository_id"))
        batch_op.drop_index(batch_op.f("ix_backup_snapshots_job_id"))

    op.drop_table("backup_snapshots")
    with op.batch_alter_table("backup_runs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_backup_runs_status"))
        batch_op.drop_index(batch_op.f("ix_backup_runs_repository_id"))
        batch_op.drop_index(batch_op.f("ix_backup_runs_job_id"))
        batch_op.drop_index(batch_op.f("ix_backup_runs_created_at"))

    op.drop_table("backup_runs")
    with op.batch_alter_table("backup_jobs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_backup_jobs_source_connection_id"))
        batch_op.drop_index(batch_op.f("ix_backup_jobs_repository_id"))
        batch_op.drop_index(batch_op.f("ix_backup_jobs_next_run_at"))

    op.drop_table("backup_jobs")
    with op.batch_alter_table("backup_repositories", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_backup_repositories_connection_id"))

    op.drop_table("backup_repositories")
