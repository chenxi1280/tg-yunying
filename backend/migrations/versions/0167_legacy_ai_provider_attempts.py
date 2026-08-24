"""Allow provider attempts before a route-v2 binding exists.

Revision ID: 0167_legacy_ai_attempt
Revises: 0166_ai_attempt_cache
"""

from alembic import op
import sqlalchemy as sa


revision = "0167_legacy_ai_attempt"
down_revision = "0166_ai_attempt_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "ai_provider_attempts",
        "route_set_id",
        existing_type=sa.String(length=36),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "ai_provider_attempts",
        "route_set_id",
        existing_type=sa.String(length=36),
        nullable=False,
    )
