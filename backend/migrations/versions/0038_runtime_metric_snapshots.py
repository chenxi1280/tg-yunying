"""runtime metric snapshots

Revision ID: 0038_runtime_metric_snapshots
Revises: 0037_capacity_dispatch_runtime
Create Date: 2026-05-16
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0038_runtime_metric_snapshots"
down_revision = "0037_capacity_dispatch_runtime"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _inspector():
    return sa.inspect(_bind())


def _table_exists(name: str) -> bool:
    return _inspector().has_table(name)


def _indexes(table_name: str) -> set[str]:
    if not _table_exists(table_name):
        return set()
    return {index["name"] for index in _inspector().get_indexes(table_name)}


def _create_index_if_missing(name: str, table_name: str, columns: list[str]) -> None:
    if name not in _indexes(table_name):
        op.create_index(name, table_name, columns)


def upgrade() -> None:
    if not _table_exists("runtime_metric_snapshots"):
        op.create_table(
            "runtime_metric_snapshots",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("metric_name", sa.String(length=120), nullable=False),
            sa.Column("dimension_type", sa.String(length=40), nullable=False, server_default="global"),
            sa.Column("dimension_id", sa.String(length=120), nullable=False, server_default="all"),
            sa.Column("metric_value", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_index_if_missing("ix_runtime_metric_snapshots_captured", "runtime_metric_snapshots", ["captured_at"])
    _create_index_if_missing("ix_runtime_metric_snapshots_metric_dimension", "runtime_metric_snapshots", ["metric_name", "dimension_type", "dimension_id"])


def downgrade() -> None:
    if _table_exists("runtime_metric_snapshots"):
        op.drop_table("runtime_metric_snapshots")
