"""Persist provider request identity on AI attempts.

Revision ID: 0182_ai_provider_request_id
Revises: 0181_runtime_storage_clone_merge
"""

from alembic import op
import sqlalchemy as sa


revision = "0182_ai_provider_request_id"
down_revision = "0181_runtime_storage_clone_merge"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(table_name)
    return column_name in {str(column["name"]) for column in columns}


def upgrade() -> None:
    if not _has_column("ai_provider_attempts", "provider_request_id"):
        op.add_column(
            "ai_provider_attempts",
            sa.Column(
                "provider_request_id",
                sa.String(length=200),
                nullable=False,
                server_default="",
            ),
        )


def downgrade() -> None:
    if _has_column("ai_provider_attempts", "provider_request_id"):
        op.drop_column("ai_provider_attempts", "provider_request_id")
