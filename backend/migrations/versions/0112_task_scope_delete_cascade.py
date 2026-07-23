"""cascade derived all-account task scope on hard delete

Revision ID: 0112_task_scope_delete_cascade
Revises: 0111_metrics_summary_anchor
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0112_task_scope_delete_cascade"
down_revision = "0111_metrics_summary_anchor"
branch_labels = None
depends_on = None


CASCADE_FOREIGN_KEYS = (
    ("task_membership_admission_items", "task_id", "tasks", "fk_membership_admission_task_cascade"),
    ("task_account_daily_coverage", "task_id", "tasks", "fk_task_daily_coverage_task_cascade"),
    (
        "task_account_daily_coverage",
        "membership_item_id",
        "task_membership_admission_items",
        "fk_task_daily_coverage_membership_cascade",
    ),
    ("task_daily_coverage_plan_cursors", "task_id", "tasks", "fk_task_daily_coverage_cursor_task_cascade"),
)


def upgrade() -> None:
    _replace_scope_foreign_keys(ondelete="CASCADE")


def downgrade() -> None:
    _replace_scope_foreign_keys(ondelete=None)


def _replace_scope_foreign_keys(*, ondelete: str | None) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table_name, column_name, target_table, constraint_name in CASCADE_FOREIGN_KEYS:
        _replace_foreign_key(table_name, column_name, target_table, constraint_name, ondelete=ondelete)


def _replace_foreign_key(
    table_name: str,
    column_name: str,
    target_table: str,
    constraint_name: str,
    *,
    ondelete: str | None,
) -> None:
    foreign_key = _scope_foreign_key(table_name, column_name, target_table)
    actual_ondelete = ((foreign_key.get("options") or {}).get("ondelete") or "").upper()
    expected_ondelete = (ondelete or "").upper()
    if actual_ondelete == expected_ondelete:
        return
    foreign_key_name = foreign_key.get("name")
    if not foreign_key_name:
        raise RuntimeError(f"unnamed foreign key: {table_name}.{column_name}")
    op.drop_constraint(foreign_key_name, table_name, type_="foreignkey")
    kwargs = {"ondelete": ondelete} if ondelete else {}
    op.create_foreign_key(constraint_name, table_name, target_table, [column_name], ["id"], **kwargs)


def _scope_foreign_key(table_name: str, column_name: str, target_table: str) -> dict:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        raise RuntimeError(f"required table missing: {table_name}")
    for foreign_key in inspector.get_foreign_keys(table_name):
        if foreign_key["constrained_columns"] == [column_name] and foreign_key["referred_table"] == target_table:
            return foreign_key
    raise RuntimeError(f"required foreign key missing: {table_name}.{column_name}")
