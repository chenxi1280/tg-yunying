"""Record cached input tokens on AI provider attempts.

Revision ID: 0166_ai_attempt_cache
Revises: 0165_online_abc_full
"""

from alembic import op
import sqlalchemy as sa


revision = "0166_ai_attempt_cache"
down_revision = "0165_online_abc_full"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(table_name)
    return column_name in {str(column["name"]) for column in columns}


def upgrade() -> None:
    if not _has_column("ai_provider_attempts", "cached_tokens"):
        op.add_column(
            "ai_provider_attempts",
            sa.Column(
                "cached_tokens",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    if _has_column("ai_provider_attempts", "cached_tokens"):
        op.drop_column("ai_provider_attempts", "cached_tokens")
