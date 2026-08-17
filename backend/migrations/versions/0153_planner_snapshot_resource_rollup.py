"""Add lossless planner wake, listener snapshot membership, and resource rollups.

Revision ID: 0153_planner_snapshot_rollup
Revises: 0152_planner_pacing_runtime
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0153_planner_snapshot_rollup"
down_revision = "0152_planner_pacing_runtime"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    columns = sa.inspect(op.get_bind()).get_columns(table_name)
    return column_name in {str(column["name"]) for column in columns}


def _add_planning_revision() -> None:
    if not _table_exists("task_planner_wake_states"):
        return
    if _column_exists("task_planner_wake_states", "planning_revision"):
        return
    op.add_column(
        "task_planner_wake_states",
        sa.Column(
            "planning_revision",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def _create_listener_snapshot_items() -> None:
    if _table_exists("listener_channel_snapshot_items"):
        return
    op.create_table(
        "listener_channel_snapshot_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "listener_source_state_id",
            sa.String(36),
            sa.ForeignKey("listener_source_state.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_revision", sa.Integer(), nullable=False),
        sa.Column(
            "channel_message_id",
            sa.Integer(),
            sa.ForeignKey("channel_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "listener_source_state_id",
            "snapshot_revision",
            "channel_message_id",
            name="uq_listener_channel_snapshot_item",
        ),
    )
    op.create_index(
        "ix_listener_channel_snapshot_current",
        "listener_channel_snapshot_items",
        ["listener_source_state_id", "snapshot_revision", "channel_message_id"],
    )


def _create_resource_rollups() -> None:
    if _table_exists("worker_runtime_resource_rollups"):
        return
    op.create_table(
        "worker_runtime_resource_rollups",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("worker_id_hash", sa.String(64), nullable=False),
        sa.Column("process_type", sa.String(40), nullable=False),
        sa.Column("release_sha", sa.String(40), nullable=False, server_default=""),
        sa.Column("bucket_at", sa.DateTime(timezone=True), nullable=False),
        *_resource_rollup_metric_columns(),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "worker_id_hash",
            "process_type",
            "release_sha",
            "bucket_at",
            name="uq_worker_runtime_resource_rollup_bucket",
        ),
    )
    op.create_index(
        "ix_worker_runtime_resource_rollup_time",
        "worker_runtime_resource_rollups",
        ["process_type", "bucket_at"],
    )


def _resource_rollup_metric_columns() -> tuple[sa.Column, ...]:
    integer_names = (
        "sample_count",
        "thread_count_max",
        "telethon_client_count_max",
    )
    bigint_names = (
        "pss_kib_p95",
        "pss_kib_max",
        "private_dirty_kib_p95",
        "anonymous_kib_p95",
        "cgroup_current_bytes_p95",
        "cgroup_current_bytes_max",
        "cgroup_event_count_max",
    )
    columns = [
        sa.Column(name, sa.Integer(), nullable=False, server_default="0")
        for name in integer_names
    ]
    columns.extend(
        sa.Column(name, sa.BigInteger(), nullable=False, server_default="0")
        for name in bigint_names
    )
    columns.append(
        sa.Column("cpu_percent_p95", sa.Float(), nullable=False, server_default="0")
    )
    return tuple(columns)


def upgrade() -> None:
    _add_planning_revision()
    _create_listener_snapshot_items()
    _create_resource_rollups()


def downgrade() -> None:
    for table_name in (
        "worker_runtime_resource_rollups",
        "listener_channel_snapshot_items",
    ):
        if _table_exists(table_name):
            op.drop_table(table_name)
    if _column_exists("task_planner_wake_states", "planning_revision"):
        op.drop_column("task_planner_wake_states", "planning_revision")
