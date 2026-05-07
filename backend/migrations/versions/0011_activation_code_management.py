"""activation code management fields

Revision ID: 0011_activation_code_management
Revises: 0010_user_subscription_usage
Create Date: 2026-05-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_activation_code_management"
down_revision = "0010_user_subscription_usage"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _column_exists(table: str, column: str) -> bool:
    return column in {item["name"] for item in sa.inspect(_bind()).get_columns(table)}


def upgrade() -> None:
    if not _table_exists("activation_codes"):
        return

    additions = [
        ("batch_no", sa.Column("batch_no", sa.String(length=24), nullable=False, server_default="")),
        ("serial_prefix", sa.Column("serial_prefix", sa.String(length=24), nullable=False, server_default="")),
    ]
    for name, column in additions:
        if not _column_exists("activation_codes", name):
            op.add_column("activation_codes", column)

    op.create_index("ix_activation_codes_status", "activation_codes", ["status"], if_not_exists=True)
    op.create_index("ix_activation_codes_plan_type", "activation_codes", ["plan_type"], if_not_exists=True)
    op.create_index("ix_activation_codes_batch_no", "activation_codes", ["batch_no"], if_not_exists=True)
    op.create_index("ix_activation_codes_created_at", "activation_codes", ["created_at"], if_not_exists=True)


def downgrade() -> None:
    if not _table_exists("activation_codes"):
        return

    op.drop_index("ix_activation_codes_created_at", table_name="activation_codes", if_exists=True)
    op.drop_index("ix_activation_codes_batch_no", table_name="activation_codes", if_exists=True)
    op.drop_index("ix_activation_codes_plan_type", table_name="activation_codes", if_exists=True)
    op.drop_index("ix_activation_codes_status", table_name="activation_codes", if_exists=True)
    for name in ["serial_prefix", "batch_no"]:
        if _column_exists("activation_codes", name):
            op.drop_column("activation_codes", name)
