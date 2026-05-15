"""capacity dispatch runtime

Revision ID: 0037_capacity_dispatch_runtime
Revises: 0036_material_version_history
Create Date: 2026-05-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0037_capacity_dispatch_runtime"
down_revision = "0036_material_version_history"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _inspector():
    return sa.inspect(_bind())


def _table_exists(name: str) -> bool:
    return _inspector().has_table(name)


def _columns(table_name: str) -> set[str]:
    if not _table_exists(table_name):
        return set()
    return {column["name"] for column in _inspector().get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    if not _table_exists(table_name):
        return set()
    return {index["name"] for index in _inspector().get_indexes(table_name)}


def _unique_constraints(table_name: str) -> set[str]:
    if not _table_exists(table_name):
        return set()
    return {constraint["name"] for constraint in _inspector().get_unique_constraints(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def _create_index_if_missing(name: str, table_name: str, columns: list[str], **kwargs) -> None:
    if name not in _indexes(table_name):
        op.create_index(name, table_name, columns, **kwargs)


def _create_unique_index_if_missing(name: str, table_name: str, columns: list[str]) -> None:
    if name not in _indexes(table_name):
        op.create_index(name, table_name, columns, unique=True)


def upgrade() -> None:
    _add_column_if_missing("actions", sa.Column("claim_owner", sa.String(length=120), nullable=False, server_default=""))
    _add_column_if_missing("actions", sa.Column("claim_token", sa.String(length=80), nullable=False, server_default=""))
    _add_column_if_missing("actions", sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True))
    _add_column_if_missing("actions", sa.Column("plan_batch_key", sa.String(length=160), nullable=True))
    _add_column_if_missing("actions", sa.Column("action_dedupe_key", sa.String(length=255), nullable=True))
    _create_index_if_missing("ix_actions_due_claim", "actions", ["status", "scheduled_at", "created_at"])
    _create_index_if_missing("ix_actions_claim_expiry", "actions", ["status", "claim_expires_at"])
    _create_index_if_missing("ix_actions_lease_recovery", "actions", ["lease_owner", "lease_expires_at"])
    _create_index_if_missing(
        "uq_actions_executing_account",
        "actions",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("status = 'executing' AND account_id IS NOT NULL"),
        sqlite_where=sa.text("status = 'executing' AND account_id IS NOT NULL"),
    )
    _create_unique_index_if_missing("uq_actions_action_dedupe_key", "actions", ["tenant_id", "action_dedupe_key"])

    if not _table_exists("execution_attempts"):
        op.create_table(
            "execution_attempts",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("action_id", sa.String(length=36), sa.ForeignKey("actions.id"), nullable=False),
            sa.Column("worker_id", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True),
            sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="before_call"),
            sa.Column("before_call_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("gateway_call_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("after_call_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("remote_message_id", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("failure_type", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("result_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("action_id", "attempt_no", name="uq_execution_attempts_action_attempt"),
        )
        op.create_index("ix_execution_attempts_unfinished", "execution_attempts", ["status", "gateway_call_started_at"])

    if not _table_exists("daily_runtime_stats"):
        op.create_table(
            "daily_runtime_stats",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("stat_date", sa.Date(), nullable=False),
            sa.Column("dimension_type", sa.String(length=40), nullable=False),
            sa.Column("dimension_id", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("metric_name", sa.String(length=80), nullable=False),
            sa.Column("metric_value", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("stat_date", "dimension_type", "dimension_id", "metric_name", name="uq_daily_runtime_stats_metric"),
        )
        op.create_index("ix_daily_runtime_stats_dimension", "daily_runtime_stats", ["dimension_type", "dimension_id"])

    if not _table_exists("runtime_cleanup_audits"):
        op.create_table(
            "runtime_cleanup_audits",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("cleanup_date", sa.Date(), nullable=False),
            sa.Column("status_counts", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("deleted_counts", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _table_exists("listener_source_state"):
        op.create_table(
            "listener_source_state",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("source_type", sa.String(length=40), nullable=False, server_default="group"),
            sa.Column("source_peer_id", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True),
            sa.Column("shard_key", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("lease_owner", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_remote_message_id", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("backfill_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("collect_window_seconds", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("tenant_id", "source_type", "source_peer_id", "account_id", name="uq_listener_source_state_source"),
        )
        op.create_index("ix_listener_source_state_claim", "listener_source_state", ["shard_key", "lease_expires_at"])


def downgrade() -> None:
    for table in ["listener_source_state", "runtime_cleanup_audits", "daily_runtime_stats", "execution_attempts"]:
        if _table_exists(table):
            op.drop_table(table)
    for index_name in ["uq_actions_executing_account", "ix_actions_lease_recovery", "ix_actions_claim_expiry", "ix_actions_due_claim"]:
        if index_name in _indexes("actions"):
            op.drop_index(index_name, table_name="actions")
    if "uq_actions_action_dedupe_key" in _indexes("actions"):
        op.drop_index("uq_actions_action_dedupe_key", table_name="actions")
    for column_name in ["action_dedupe_key", "plan_batch_key", "claim_expires_at", "claim_token", "claim_owner"]:
        if column_name in _columns("actions"):
            op.drop_column("actions", column_name)
