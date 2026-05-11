"""rule set versions

Revision ID: 0024_rule_set_versions
Revises: 0023_task_center_soft_delete
Create Date: 2026-05-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0024_rule_set_versions"
down_revision = "0023_task_center_soft_delete"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _index_names(table: str) -> set[str]:
    if not _table_exists(table):
        return set()
    return {item["name"] for item in sa.inspect(_bind()).get_indexes(table)}


def upgrade() -> None:
    if not _table_exists("rule_sets"):
        op.create_table(
            "rule_sets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
            sa.Column("active_version_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "name", name="uq_rule_sets_tenant_name"),
        )
    if not _table_exists("rule_set_versions"):
        op.create_table(
            "rule_set_versions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("rule_set_id", sa.Integer(), sa.ForeignKey("rule_sets.id"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="draft"),
            sa.Column("filters", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("transforms", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("routing", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("account_strategy", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("rate_limits", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("retry_policy", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("created_by", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("published_by", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("rule_set_id", "version", name="uq_rule_set_versions_set_version"),
        )
    for name, table, columns in [
        ("idx_rule_sets_tenant", "rule_sets", ["tenant_id", "status"]),
        ("idx_rule_set_versions_set", "rule_set_versions", ["rule_set_id", "status"]),
    ]:
        if name not in _index_names(table):
            op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table in [
        ("idx_rule_set_versions_set", "rule_set_versions"),
        ("idx_rule_sets_tenant", "rule_sets"),
    ]:
        if name in _index_names(table):
            op.drop_index(name, table_name=table)
    for table in ["rule_set_versions", "rule_sets"]:
        if _table_exists(table):
            op.drop_table(table)
