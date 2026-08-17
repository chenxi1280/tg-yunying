"""Add planner projections, source pacing admission, and resource samples.

Revision ID: 0152_planner_pacing_runtime
Revises: 0151_group_slot_pacing_due
"""

from __future__ import annotations

from collections.abc import Callable

from alembic import op
import sqlalchemy as sa


revision = "0152_planner_pacing_runtime"
down_revision = "0151_group_slot_pacing_due"
branch_labels = None
depends_on = None
ColumnFactory = Callable[[], sa.Column]

OWNER_TABLES = (
    "task_group_daily_message_slots",
    "comment_fulfillment_obligations",
    "reaction_fulfillment_obligations",
    "view_fulfillment_obligations",
)
EXISTING_INDEXES = (
    (
        "task_membership_admission_items",
        "ix_membership_admission_planner_selection",
        "task_id, eligibility_rank, planner_last_selected_at, id",
    ),
    (
        "listener_source_state",
        "ix_listener_source_state_snapshot",
        "source_type, snapshot_status, fresh_until_at, id",
    ),
    *(
        (
            table_name,
            f"ix_{table_name}_source_cursor",
            "tenant_id, pacing_source_key_hash, pacing_period_key, release_not_before_at, id",
        )
        for table_name in OWNER_TABLES
    ),
)


def _existing_columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return set()
    return {str(column["name"]) for column in inspector.get_columns(table_name)}


def _add_columns(table_name: str, factories: tuple[ColumnFactory, ...]) -> None:
    existing = _existing_columns(table_name)
    if not existing:
        return
    for factory in factories:
        column = factory()
        if str(column.name) not in existing:
            op.add_column(table_name, column)


def _drop_columns(table_name: str, factories: tuple[ColumnFactory, ...]) -> None:
    existing = _existing_columns(table_name)
    for factory in reversed(factories):
        column = factory()
        if str(column.name) in existing:
            op.drop_column(table_name, str(column.name))


def _runtime_summary_columns() -> tuple[ColumnFactory, ...]:
    return (
        lambda: sa.Column("lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
        lambda: sa.Column("blocker_revision", sa.Integer(), nullable=False, server_default="0"),
    )


def _membership_columns() -> tuple[ColumnFactory, ...]:
    return (
        lambda: sa.Column("eligibility_rank", sa.Integer(), nullable=False, server_default="100"),
        lambda: sa.Column("eligibility_revision", sa.Integer(), nullable=False, server_default="1"),
        lambda: sa.Column("planner_last_selected_at", sa.DateTime(timezone=True), nullable=True),
    )


def _listener_columns() -> tuple[ColumnFactory, ...]:
    return (
        lambda: sa.Column("snapshot_revision", sa.Integer(), nullable=False, server_default="0"),
        lambda: sa.Column("snapshot_status", sa.String(32), nullable=False, server_default="pending"),
        lambda: sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        lambda: sa.Column("fresh_until_at", sa.DateTime(timezone=True), nullable=True),
        lambda: sa.Column("next_probe_at", sa.DateTime(timezone=True), nullable=True),
        lambda: sa.Column("last_error_code", sa.String(80), nullable=False, server_default=""),
    )


def _owner_identity_columns() -> tuple[ColumnFactory, ...]:
    return (
        lambda: sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=True),
        lambda: sa.Column("pacing_period_key", sa.String(80), nullable=True),
        lambda: sa.Column("pacing_source_key_hash", sa.String(64), nullable=True),
    )


def _create_task_planner_wake_states() -> None:
    if _table_exists("task_planner_wake_states"):
        return
    op.create_table(
        "task_planner_wake_states",
        _id_column(),
        _tenant_column(),
        _task_column(),
        sa.Column("lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("wake_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("planned_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("not_before_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason_code", sa.String(80), nullable=False, server_default=""),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        _updated_column(),
        sa.UniqueConstraint("tenant_id", "task_id", name="uq_task_planner_wake_task"),
    )
    op.create_index(
        "ix_task_planner_wake_due",
        "task_planner_wake_states",
        ["not_before_at", "wake_revision"],
    )


def _create_task_admission_projections() -> None:
    if _table_exists("task_admission_projections"):
        return
    op.create_table(
        "task_admission_projections",
        _id_column(),
        _tenant_column(),
        _task_column(),
        sa.Column("lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("scope_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("item_revision", sa.Integer(), nullable=False, server_default="0"),
        *_count_columns(),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        _updated_column(),
        sa.UniqueConstraint(
            "tenant_id", "task_id", "lifecycle_epoch",
            name="uq_task_admission_projection_epoch",
        ),
    )
    op.create_index(
        "ix_task_admission_projection_task",
        "task_admission_projections",
        ["tenant_id", "task_id", "lifecycle_epoch"],
    )


def _create_task_runtime_active_blockers() -> None:
    if _table_exists("task_runtime_active_blockers"):
        return
    op.create_table(
        "task_runtime_active_blockers",
        _id_column(),
        _tenant_column(),
        _task_column(),
        sa.Column("lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("blocker_domain", sa.String(40), nullable=False),
        sa.Column("scope_key_hash", sa.String(64), nullable=False),
        sa.Column("blocker_code", sa.String(80), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False, server_default=""),
        sa.Column("source_id_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("source_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        _updated_column(),
        sa.UniqueConstraint(
            "tenant_id", "task_id", "lifecycle_epoch", "blocker_domain", "scope_key_hash",
            name="uq_task_runtime_active_blocker_scope",
        ),
    )
    op.create_index(
        "ix_task_runtime_active_blocker_task",
        "task_runtime_active_blockers",
        ["tenant_id", "task_id", "lifecycle_epoch", "updated_at"],
    )


def _create_task_source_subscriptions() -> None:
    if _table_exists("task_source_subscriptions"):
        return
    op.create_table(
        "task_source_subscriptions",
        _id_column(),
        _tenant_column(),
        _task_column(),
        sa.Column("lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_peer_hash", sa.String(64), nullable=False),
        sa.Column(
            "listener_source_state_id",
            sa.String(36),
            sa.ForeignKey("listener_source_state.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("required_snapshot_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(32), nullable=False, server_default="pending"),
        _created_column(),
        _updated_column(),
        sa.UniqueConstraint(
            "tenant_id", "task_id", "lifecycle_epoch", "source_type", "source_peer_hash",
            name="uq_task_source_subscription_scope",
        ),
    )
    op.create_index(
        "ix_task_source_subscription_listener",
        "task_source_subscriptions",
        ["listener_source_state_id", "state"],
    )


def _create_source_pacing_states() -> None:
    if _table_exists("source_pacing_states"):
        return
    op.create_table(
        "source_pacing_states",
        _id_column(),
        _tenant_column(),
        sa.Column("pacing_domain", sa.String(40), nullable=False),
        sa.Column("source_key_hash", sa.String(64), nullable=False),
        sa.Column("next_call_not_before_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_call_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_source_gap_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        _created_column(),
        _updated_column(),
        sa.UniqueConstraint(
            "tenant_id", "pacing_domain", "source_key_hash",
            name="uq_source_pacing_state_source",
        ),
    )
    op.create_index(
        "ix_source_pacing_state_timeline",
        "source_pacing_states",
        ["tenant_id", "pacing_domain", "source_key_hash"],
    )


def _create_source_pacing_admissions() -> None:
    if _table_exists("source_pacing_admissions"):
        return
    op.create_table(
        "source_pacing_admissions",
        _id_column(),
        sa.Column("admission_key", sa.String(160), nullable=False),
        _tenant_column(),
        _task_column(),
        sa.Column("lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "source_pacing_state_id", sa.String(36),
            sa.ForeignKey("source_pacing_states.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("owner_type", sa.String(50), nullable=False),
        sa.Column("owner_id", sa.String(80), nullable=False),
        sa.Column("action_id", sa.String(36), sa.ForeignKey("actions.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "attempt_id", sa.String(36),
            sa.ForeignKey("execution_attempts.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("pacing_period_key", sa.String(80), nullable=False),
        sa.Column("pacing_plan_hash", sa.String(64), nullable=False),
        sa.Column("planned_release_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("call_not_before_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_gap_seconds", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="reserved"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        _created_column(),
        _updated_column(),
        sa.UniqueConstraint("admission_key", name="uq_source_pacing_admission_key"),
    )
    op.create_index(
        "ix_source_pacing_admission_due",
        "source_pacing_admissions",
        ["tenant_id", "state", "call_not_before_at"],
    )
    op.create_index(
        "ix_source_pacing_admission_action",
        "source_pacing_admissions",
        ["action_id", "attempt_id"],
    )


def _create_worker_runtime_resource_samples() -> None:
    if _table_exists("worker_runtime_resource_samples"):
        return
    op.create_table(
        "worker_runtime_resource_samples",
        _id_column(),
        sa.Column("worker_id_hash", sa.String(64), nullable=False),
        sa.Column("process_type", sa.String(40), nullable=False),
        sa.Column("release_sha", sa.String(40), nullable=False, server_default=""),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sample_interval_seconds", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("cgroup_version", sa.Integer(), nullable=False, server_default="0"),
        *_resource_count_columns(),
        sa.Column("cpu_percent", sa.Float(), nullable=False, server_default="0"),
        sa.Column("thread_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("telethon_client_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("drain_metrics", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("state", sa.String(24), nullable=False, server_default="fresh"),
    )
    op.create_index(
        "ix_worker_runtime_resource_role_time",
        "worker_runtime_resource_samples",
        ["process_type", "captured_at"],
    )
    op.create_index(
        "ix_worker_runtime_resource_worker_time",
        "worker_runtime_resource_samples",
        ["worker_id_hash", "captured_at"],
    )


def _count_columns() -> tuple[sa.Column, ...]:
    names = ("candidate", "joined", "pending", "failed", "unknown", "ready")
    return tuple(
        sa.Column(f"{name}_count", sa.Integer(), nullable=False, server_default="0")
        for name in names
    )


def _resource_count_columns() -> tuple[sa.Column, ...]:
    names = (
        "rss_kib", "pss_kib", "private_dirty_kib", "anonymous_kib",
        "anon_huge_pages_kib", "cgroup_current_bytes", "cgroup_peak_bytes",
        "cgroup_limit_bytes", "cgroup_event_count",
    )
    return tuple(
        sa.Column(name, sa.BigInteger(), nullable=False, server_default="0")
        for name in names
    )


def _id_column() -> sa.Column:
    return sa.Column("id", sa.String(36), primary_key=True)


def _tenant_column() -> sa.Column:
    return sa.Column(
        "tenant_id", sa.Integer(),
        sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False,
    )


def _task_column() -> sa.Column:
    return sa.Column(
        "task_id", sa.String(36),
        sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False,
    )


def _created_column() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True),
        nullable=False, server_default=sa.func.now(),
    )


def _updated_column() -> sa.Column:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True),
        nullable=False, server_default=sa.func.now(),
    )


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _create_existing_indexes() -> None:
    for table_name, index_name, columns in EXISTING_INDEXES:
        if _index_ready(table_name, index_name, columns):
            continue
        _execute_index_ddl(_create_index_sql(table_name, index_name, columns))


def _drop_existing_indexes() -> None:
    for table_name, index_name, _columns in reversed(EXISTING_INDEXES):
        if not _index_exists(table_name, index_name):
            continue
        concurrent = "CONCURRENTLY " if _is_postgres() else ""
        _execute_index_ddl(f"DROP INDEX {concurrent}IF EXISTS {index_name}")


def _index_ready(table_name: str, index_name: str, columns: str) -> bool:
    if not _table_exists(table_name):
        return True
    available = _existing_columns(table_name)
    if not set(columns.replace(" ", "").split(",")).issubset(available):
        return True
    if not _is_postgres():
        return _index_exists(table_name, index_name)
    state = _postgres_index_state(table_name, index_name)
    if state is False:
        _execute_index_ddl(f"DROP INDEX CONCURRENTLY {index_name}")
    return state is True


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return index_name in {
        str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def _create_index_sql(table_name: str, index_name: str, columns: str) -> str:
    modifier = "CONCURRENTLY IF NOT EXISTS" if _is_postgres() else "IF NOT EXISTS"
    return f"CREATE INDEX {modifier} {index_name} ON {table_name} ({columns})"


def _execute_index_ddl(statement: str) -> None:
    if not _is_postgres():
        op.execute(sa.text(statement))
        return
    with op.get_context().autocommit_block():
        op.execute(sa.text(statement))


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _postgres_index_state(table_name: str, index_name: str) -> bool | None:
    row = op.get_bind().execute(sa.text("""
        SELECT index_meta.indisvalid
        FROM pg_index AS index_meta
        JOIN pg_class AS index_class ON index_class.oid = index_meta.indexrelid
        JOIN pg_class AS table_class ON table_class.oid = index_meta.indrelid
        JOIN pg_namespace AS namespace ON namespace.oid = table_class.relnamespace
        WHERE index_class.relname = :index_name
          AND table_class.relname = :table_name
          AND namespace.nspname = current_schema()
    """), {"index_name": index_name, "table_name": table_name}).scalar_one_or_none()
    return bool(row) if row is not None else None


def upgrade() -> None:
    _add_columns("task_runtime_summary", _runtime_summary_columns())
    _add_columns("task_membership_admission_items", _membership_columns())
    _add_columns("listener_source_state", _listener_columns())
    for table_name in OWNER_TABLES:
        _add_columns(table_name, _owner_identity_columns())
    _create_task_planner_wake_states()
    _create_task_admission_projections()
    _create_task_runtime_active_blockers()
    _create_task_source_subscriptions()
    _create_source_pacing_states()
    _create_source_pacing_admissions()
    _create_worker_runtime_resource_samples()
    _create_existing_indexes()


def downgrade() -> None:
    _drop_existing_indexes()
    for table_name in (
        "worker_runtime_resource_samples",
        "source_pacing_admissions",
        "source_pacing_states",
        "task_source_subscriptions",
        "task_runtime_active_blockers",
        "task_admission_projections",
        "task_planner_wake_states",
    ):
        if _table_exists(table_name):
            op.drop_table(table_name)
    for table_name in reversed(OWNER_TABLES):
        _drop_columns(table_name, _owner_identity_columns())
    _drop_columns("listener_source_state", _listener_columns())
    _drop_columns("task_membership_admission_items", _membership_columns())
    _drop_columns("task_runtime_summary", _runtime_summary_columns())
