"""Index deadline settlement and source cursor reconciliation lookups.

Revision ID: 0154_account_pacing_action_state
Revises: 0153_planner_snapshot_rollup
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0154_account_pacing_action_state"
down_revision = "0153_planner_snapshot_rollup"
branch_labels = None
depends_on = None
INDEX_SPECS = (
    (
        "ix_account_pacing_reservation_action_state",
        "account_pacing_reservations",
        "action_id, state",
    ),
    (
        "ix_source_pacing_admission_state_due",
        "source_pacing_admissions",
        "source_pacing_state_id, state, call_not_before_at",
    ),
)


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table_name):
        return False
    return index_name in {
        str(index["name"])
        for index in inspector.get_indexes(table_name)
    }


def upgrade() -> None:
    for index_name, table_name, columns in INDEX_SPECS:
        if _index_exists(table_name, index_name):
            continue
        _create_index(index_name, table_name, columns)


def _create_index(index_name: str, table_name: str, columns: str) -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                f"CREATE INDEX CONCURRENTLY {index_name} "
                f"ON {table_name} ({columns})"
            )
        return
    op.create_index(index_name, table_name, columns.split(", "))


def downgrade() -> None:
    for index_name, table_name, _columns in reversed(INDEX_SPECS):
        if not _index_exists(table_name, index_name):
            continue
        _drop_index(index_name, table_name)


def _drop_index(index_name: str, table_name: str) -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(f"DROP INDEX CONCURRENTLY {index_name}")
        return
    op.drop_index(index_name, table_name=table_name)
