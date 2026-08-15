"""Add durable deterministic pacing and generation audit fields.

Revision ID: 0149_pacing_slot_fields
Revises: 0148_account_batch_login
"""

from __future__ import annotations

from collections.abc import Callable

from alembic import op
import sqlalchemy as sa


revision = "0149_pacing_slot_fields"
down_revision = "0148_account_batch_login"
branch_labels = None
depends_on = None
ColumnFactory = Callable[[], sa.Column]
TIMELINE_INDEXES = (
    (
        "actions",
        "ix_actions_account_pacing_timeline",
        ["tenant_id", "account_id", "scheduled_at"],
        "status IN ('pending','claiming','executing','retryable_failed','unknown_after_send') "
        "AND scheduled_at IS NOT NULL",
    ),
    (
        "execution_attempts",
        "ix_execution_attempts_account_pacing_timeline",
        ["tenant_id", "account_id", "after_call_at"],
        "status = 'success' AND after_call_at IS NOT NULL",
    ),
    (
        "fulfillment_remote_facts",
        "ix_fulfillment_remote_fact_account_timeline",
        ["tenant_id", "observed_at", "action_id"],
        "fact_kind IN ('remote_message_observed','view_observed','reaction_observed')",
    ),
    (
        "fulfillment_remote_facts",
        "ix_fulfillment_remote_fact_action_typed",
        ["tenant_id", "action_id", "fact_kind", "observed_at"],
        "fact_kind IN ('remote_message_observed','view_observed','reaction_observed')",
    ),
)


def _action_columns() -> tuple[ColumnFactory, ...]:
    return (
        lambda: sa.Column("pacing_slot_key", sa.String(255), nullable=True),
        lambda: sa.Column("pacing_due_at", sa.DateTime(timezone=True), nullable=True),
        lambda: sa.Column("pacing_contract_version", sa.String(48), nullable=True),
        lambda: sa.Column("pacing_plan_hash", sa.String(64), nullable=True),
        lambda: sa.Column("pacing_slot_ordinal", sa.Integer(), nullable=True),
        lambda: sa.Column("release_not_before_at", sa.DateTime(timezone=True), nullable=True),
        lambda: sa.Column("effective_claim_at", sa.DateTime(timezone=True), nullable=True),
        lambda: sa.Column("assignment_revision", sa.Integer(), nullable=False, server_default="1"),
        lambda: sa.Column("intent_revision", sa.Integer(), nullable=False, server_default="1"),
        lambda: sa.Column("candidate_hash", sa.String(64), nullable=False, server_default=""),
    )


def _owner_columns(*, include_due: bool = True) -> tuple[ColumnFactory, ...]:
    columns: list[ColumnFactory] = [
        lambda: sa.Column("pacing_contract_version", sa.String(48), nullable=True),
        lambda: sa.Column("pacing_plan_hash", sa.String(64), nullable=True),
    ]
    if include_due:
        columns.append(lambda: sa.Column("pacing_due_at", sa.DateTime(timezone=True), nullable=True))
    columns.extend((
        lambda: sa.Column("release_not_before_at", sa.DateTime(timezone=True), nullable=True),
        lambda: sa.Column("pacing_slot_ordinal", sa.Integer(), nullable=True),
        lambda: sa.Column("pacing_plan_total", sa.Integer(), nullable=True),
    ))
    return tuple(columns)


def _generation_columns() -> tuple[ColumnFactory, ...]:
    return (
        lambda: sa.Column("generation_not_before_at", sa.DateTime(timezone=True), nullable=True),
        lambda: sa.Column("context_snapshot_hash", sa.String(64), nullable=False, server_default=""),
        lambda: sa.Column("assignment_revision", sa.Integer(), nullable=False, server_default="1"),
        lambda: sa.Column("intent_revision", sa.Integer(), nullable=False, server_default="1"),
        lambda: sa.Column("candidate_hash", sa.String(64), nullable=False, server_default=""),
        lambda: sa.Column("evaluator_evidence", sa.JSON(), nullable=False, server_default="{}"),
    )


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _add_columns(table_name: str, factories: tuple[ColumnFactory, ...]) -> None:
    if not _table_exists(table_name):
        return
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}
    for factory in factories:
        column = factory()
        if column.name not in existing:
            op.add_column(table_name, column)


def _drop_columns(table_name: str, factories: tuple[ColumnFactory, ...]) -> None:
    if not _table_exists(table_name):
        return
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}
    for factory in reversed(factories):
        column = factory()
        if column.name in existing:
            op.drop_column(table_name, str(column.name))


def _create_account_reservations() -> None:
    if _table_exists("account_pacing_reservations"):
        return
    op.create_table(
        "account_pacing_reservations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pacing_slot_key", sa.String(255), nullable=False),
        sa.Column("policy_version", sa.String(48), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("release_not_before_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_claim_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("action_id", sa.String(36), sa.ForeignKey("actions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("state", sa.String(24), nullable=False, server_default="reserved"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "tenant_id", "account_id", "pacing_slot_key",
            name="uq_account_pacing_reservation_slot",
        ),
    )
    op.create_index(
        "ix_account_pacing_reservation_timeline",
        "account_pacing_reservations",
        ["tenant_id", "account_id", "state", "effective_claim_at"],
    )


def _create_timeline_indexes() -> None:
    for definition in TIMELINE_INDEXES:
        table_name, index_name, columns, _predicate = definition
        if not _table_exists(table_name):
            continue
        inspector = sa.inspect(op.get_bind())
        available = {column["name"] for column in inspector.get_columns(table_name)}
        if not set(columns).issubset(available):
            continue
        if index_name in {index["name"] for index in inspector.get_indexes(table_name)}:
            continue
        _execute_timeline_ddl(_timeline_create_sql(definition, postgres=_is_postgres()))


def _drop_timeline_indexes() -> None:
    for table_name, index_name, _columns, _predicate in reversed(TIMELINE_INDEXES):
        if not _table_exists(table_name):
            continue
        names = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}
        if index_name in names:
            concurrent = "CONCURRENTLY " if _is_postgres() else ""
            _execute_timeline_ddl(f"DROP INDEX {concurrent}{index_name}")


def _timeline_create_sql(definition, *, postgres: bool) -> str:
    table_name, index_name, columns, predicate = definition
    mode = "CONCURRENTLY" if postgres else "IF NOT EXISTS"
    return (
        f"CREATE INDEX {mode} {index_name} ON {table_name} "
        f"({', '.join(columns)}) WHERE {predicate}"
    )


def _execute_timeline_ddl(statement: str) -> None:
    if not _is_postgres():
        op.execute(sa.text(statement))
        return
    with op.get_context().autocommit_block():
        op.execute(sa.text(statement))


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    _add_columns("actions", _action_columns())
    _add_columns("comment_fulfillment_obligations", _owner_columns())
    _add_columns("reaction_fulfillment_obligations", _owner_columns())
    _add_columns("view_fulfillment_obligations", _owner_columns())
    _add_columns("task_group_daily_message_slots", _owner_columns(include_due=False))
    _add_columns("generation_jobs", _generation_columns())
    _create_account_reservations()
    _create_timeline_indexes()


def downgrade() -> None:
    _drop_timeline_indexes()
    if _table_exists("account_pacing_reservations"):
        op.drop_table("account_pacing_reservations")
    _drop_columns("generation_jobs", _generation_columns())
    _drop_columns("task_group_daily_message_slots", _owner_columns(include_due=False))
    _drop_columns("view_fulfillment_obligations", _owner_columns())
    _drop_columns("reaction_fulfillment_obligations", _owner_columns())
    _drop_columns("comment_fulfillment_obligations", _owner_columns())
    _drop_columns("actions", _action_columns())
