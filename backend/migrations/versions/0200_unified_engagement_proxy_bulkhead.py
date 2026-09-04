"""Add proxy bulkhead keys to unified engagement leases.

Revision ID: 0200_engagement_proxy_bulkhead
Revises: 0199_unified_engagement_planning
"""

from alembic import op
import sqlalchemy as sa


revision = "0200_engagement_proxy_bulkhead"
down_revision = "0199_unified_engagement_planning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = "account_pool_concurrency_leases"
    op.add_column(
        table,
        sa.Column("proxy_route_key", sa.String(80), nullable=False, server_default=""),
    )
    op.add_column(
        table,
        sa.Column("proxy_egress_key", sa.String(120), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_pool_concurrency_proxy_route",
        table,
        ["tenant_id", "proxy_route_key", "state"],
    )
    op.create_index(
        "ix_pool_concurrency_proxy_egress",
        table,
        ["tenant_id", "proxy_egress_key", "state"],
    )


def downgrade() -> None:
    table = "account_pool_concurrency_leases"
    op.drop_index("ix_pool_concurrency_proxy_egress", table_name=table)
    op.drop_index("ix_pool_concurrency_proxy_route", table_name=table)
    op.drop_column(table, "proxy_egress_key")
    op.drop_column(table, "proxy_route_key")
