"""System monitoring and alerting: samples, checks, rules, alerts, channels (Phase 10).

Seeds the built-in alert rules. No permission tokens change: monitoring and
alerts use the existing ``system:read`` (admin, operator) and
``system:write`` (admin) tokens.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Built-in rules (name, metric, threshold, duration seconds, severity). Kept
# inline so the migration never imports application code. They can be tuned
# or disabled through the API but not deleted.
BUILTIN_RULES: list[tuple[str, str, float | None, int, str]] = [
    ("Disk usage high", "disk_percent", 80.0, 300, "warning"),
    ("Disk almost full", "disk_percent", 90.0, 300, "critical"),
    ("Memory usage high", "memory_percent", 90.0, 600, "warning"),
    ("Swap usage high", "swap_percent", 50.0, 900, "warning"),
    ("Load per core high", "load_per_core", 2.0, 900, "warning"),
    ("RAID array degraded", "raid_degraded", None, 0, "critical"),
    ("RAID member failed", "raid_failed_member", None, 0, "critical"),
    ("SMART health failed", "smart_failed", None, 0, "critical"),
    ("Service down", "service_down", None, 120, "critical"),
    ("Privileged helper unavailable", "helper_unavailable", None, 600, "warning"),
]


def _seed_rules() -> None:
    rules = sa.table(
        "alert_rules",
        sa.column("name", sa.String),
        sa.column("metric", sa.String),
        sa.column("comparison", sa.String),
        sa.column("threshold", sa.Float),
        sa.column("duration_seconds", sa.Integer),
        sa.column("severity", sa.String),
        sa.column("enabled", sa.Boolean),
        sa.column("notify", sa.Boolean),
        sa.column("builtin", sa.Boolean),
    )
    op.bulk_insert(
        rules,
        [
            {
                "name": name,
                "metric": metric,
                "comparison": "gt",
                "threshold": threshold,
                "duration_seconds": duration,
                "severity": severity,
                "enabled": True,
                "notify": True,
                "builtin": True,
            }
            for name, metric, threshold, duration, severity in BUILTIN_RULES
        ],
    )


def upgrade() -> None:
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("metric", sa.String(length=32), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=True),
        sa.Column("comparison", sa.String(length=4), server_default="gt", nullable=False),
        sa.Column("threshold", sa.Float(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), server_default="0", nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("notify", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("builtin", sa.Boolean(), server_default="0", nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_rules")),
        sa.UniqueConstraint("name", name=op.f("uq_alert_rules_name")),
    )
    op.create_table(
        "metric_samples",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cpu_percent", sa.Float(), nullable=False),
        sa.Column("load1", sa.Float(), nullable=True),
        sa.Column("load_per_core", sa.Float(), nullable=True),
        sa.Column("memory_percent", sa.Float(), nullable=False),
        sa.Column("swap_percent", sa.Float(), nullable=False),
        sa.Column("net_bytes_sent", sa.BigInteger(), nullable=False),
        sa.Column("net_bytes_recv", sa.BigInteger(), nullable=False),
        sa.Column("disks", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metric_samples")),
    )
    with op.batch_alter_table("metric_samples", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_metric_samples_sampled_at"), ["sampled_at"], unique=False
        )

    op.create_table(
        "monitor_checks",
        sa.Column("key", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("target", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_monitor_checks")),
    )
    with op.batch_alter_table("monitor_checks", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_monitor_checks_kind"), ["kind"], unique=False)

    op.create_table(
        "alert_channels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("secrets_encrypted", sa.Text(), nullable=True),
        sa.Column("min_severity", sa.String(length=16), server_default="warning", nullable=False),
        sa.Column("allow_insecure", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("send_resolved", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("fk_alert_channels_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_channels")),
        sa.UniqueConstraint("name", name=op.f("uq_alert_channels_name")),
    )
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("target", sa.String(length=255), server_default="", nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("firing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.Integer(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["acknowledged_by"],
            ["users.id"],
            name=op.f("fk_alerts_acknowledged_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["alert_rules.id"],
            name=op.f("fk_alerts_rule_id_alert_rules"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alerts")),
    )
    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_alerts_resolved_at"), ["resolved_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_alerts_rule_id"), ["rule_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_alerts_state"), ["state"], unique=False)

    op.create_table(
        "alert_notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("alert_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["alerts.id"],
            name=op.f("fk_alert_notifications_alert_id_alerts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["alert_channels.id"],
            name=op.f("fk_alert_notifications_channel_id_alert_channels"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_notifications")),
    )
    with op.batch_alter_table("alert_notifications", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_alert_notifications_alert_id"), ["alert_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_alert_notifications_channel_id"), ["channel_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_alert_notifications_status"), ["status"], unique=False)

    _seed_rules()


def downgrade() -> None:
    with op.batch_alter_table("alert_notifications", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_alert_notifications_status"))
        batch_op.drop_index(batch_op.f("ix_alert_notifications_channel_id"))
        batch_op.drop_index(batch_op.f("ix_alert_notifications_alert_id"))

    op.drop_table("alert_notifications")
    with op.batch_alter_table("alerts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_alerts_state"))
        batch_op.drop_index(batch_op.f("ix_alerts_rule_id"))
        batch_op.drop_index(batch_op.f("ix_alerts_resolved_at"))

    op.drop_table("alerts")
    op.drop_table("alert_channels")
    with op.batch_alter_table("monitor_checks", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_monitor_checks_kind"))

    op.drop_table("monitor_checks")
    with op.batch_alter_table("metric_samples", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_metric_samples_sampled_at"))

    op.drop_table("metric_samples")
    op.drop_table("alert_rules")
