"""Enable multiple AI providers and explicit tenant route fallback.

Revision ID: 0161_provider_failover
Revises: 0160_abc_canary
"""

from alembic import op
import sqlalchemy as sa


revision = "0161_provider_failover"
down_revision = "0160_abc_canary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "uq_ai_provider_single_active",
        table_name="ai_providers",
        if_exists=True,
    )
    op.add_column(
        "tenant_ai_settings",
        sa.Column(
            "ai_provider_route_fallback_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "tenant_ai_settings",
        "ai_provider_route_fallback_enabled",
    )
    op.create_index(
        "uq_ai_provider_single_active",
        "ai_providers",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )
