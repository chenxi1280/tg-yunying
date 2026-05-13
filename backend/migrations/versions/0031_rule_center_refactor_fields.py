"""rule center refactor fields

Revision ID: 0031_rule_center_refactor_fields
Revises: 0030_repair_sched_limits
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0031_rule_center_refactor_fields"
down_revision = "0030_repair_sched_limits"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _column_names(table: str) -> set[str]:
    if not _table_exists(table):
        return set()
    return {item["name"] for item in sa.inspect(_bind()).get_columns(table)}


def _add_json_column(table: str, name: str) -> None:
    op.add_column(table, sa.Column(name, sa.JSON(), nullable=False, server_default="{}"))


def upgrade() -> None:
    if _table_exists("rule_sets"):
        columns = _column_names("rule_sets")
        if "task_types" not in columns:
            op.add_column("rule_sets", sa.Column("task_types", sa.JSON(), nullable=False, server_default="[]"))
        if "default_policy" not in columns:
            _add_json_column("rule_sets", "default_policy")
    if _table_exists("rule_set_versions"):
        columns = _column_names("rule_set_versions")
        if "version_note" not in columns:
            op.add_column("rule_set_versions", sa.Column("version_note", sa.Text(), nullable=False, server_default=""))
        if "output_checks" not in columns:
            _add_json_column("rule_set_versions", "output_checks")


def downgrade() -> None:
    for table, column in [
        ("rule_set_versions", "output_checks"),
        ("rule_set_versions", "version_note"),
        ("rule_sets", "default_policy"),
        ("rule_sets", "task_types"),
    ]:
        if column in _column_names(table):
            op.drop_column(table, column)
