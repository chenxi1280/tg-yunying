"""enforce unique active rank deboost runtime nodes

Revision ID: 0089_rank_deboost_runtime_index
Revises: 0088_ai_group_daily_coverage
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0089_rank_deboost_runtime_index"
down_revision = "0088_ai_group_daily_coverage"
branch_labels = None
depends_on = None

TABLE_NAME = "account_group_proxy_bindings"
INDEX_NAME = "uq_account_group_proxy_binding_active_node"
ACTIVE_WHERE = "status = 'active' AND unbound_at IS NULL"


def upgrade() -> None:
    if not _has_required_binding_columns() or INDEX_NAME in _index_names():
        return
    _mark_duplicate_active_nodes_for_revalidation()
    op.create_index(
        INDEX_NAME,
        TABLE_NAME,
        ["tenant_id", "proxy_airport_node_id", "status"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_WHERE),
        sqlite_where=sa.text(ACTIVE_WHERE),
    )


def downgrade() -> None:
    if _has_table() and INDEX_NAME in _index_names():
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)


def _mark_duplicate_active_nodes_for_revalidation() -> None:
    bind = op.get_bind()
    bindings = sa.Table(TABLE_NAME, sa.MetaData(), autoload_with=bind)
    duplicate_groups = bind.execute(
        sa.select(bindings.c.tenant_id, bindings.c.proxy_airport_node_id)
        .where(bindings.c.status == "active", bindings.c.unbound_at.is_(None))
        .group_by(bindings.c.tenant_id, bindings.c.proxy_airport_node_id)
        .having(sa.func.count(bindings.c.id) > 1)
    ).all()
    for tenant_id, node_id in duplicate_groups:
        bind.execute(
            bindings.update()
            .where(
                bindings.c.tenant_id == tenant_id,
                bindings.c.proxy_airport_node_id == node_id,
                bindings.c.status == "active",
                bindings.c.unbound_at.is_(None),
            )
            .values(status="needs_runtime_proxy")
        )


def _has_required_binding_columns() -> bool:
    if not _has_table():
        return False
    required = {"id", "tenant_id", "proxy_airport_node_id", "status", "unbound_at"}
    return required <= {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE_NAME)}


def _index_names() -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(TABLE_NAME)}


def _has_table() -> bool:
    return TABLE_NAME in sa.inspect(op.get_bind()).get_table_names()
