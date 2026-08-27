"""Persist observed channel Reaction capabilities.

Revision ID: 0170_channel_reaction_cap
Revises: 0169_post_login_stage_order
"""

from alembic import op
import sqlalchemy as sa


revision = "0170_channel_reaction_cap"
down_revision = "0169_post_login_stage_order"
branch_labels = None
depends_on = None


TABLE = "operation_targets"


def _column_names() -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(TABLE)
    }


def upgrade() -> None:
    columns = _column_names()
    if "reaction_capability_mode" not in columns:
        op.add_column(
            TABLE,
            sa.Column(
                "reaction_capability_mode",
                sa.String(length=16),
                nullable=False,
                server_default="unknown",
            ),
        )
    if "available_reactions" not in columns:
        op.add_column(
            TABLE,
            sa.Column(
                "available_reactions",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
        )


def downgrade() -> None:
    columns = _column_names()
    if "available_reactions" in columns:
        op.drop_column(TABLE, "available_reactions")
    if "reaction_capability_mode" in columns:
        op.drop_column(TABLE, "reaction_capability_mode")
