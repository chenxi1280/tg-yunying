"""Persist adapter references to source journey plans.

Revision ID: 0214_source_journey_wiring
Revises: 0213_unowned_outbound_activity
"""

from alembic import op
import sqlalchemy as sa


revision = "0214_source_journey_wiring"
down_revision = "0213_unowned_outbound_activity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("channel_comment_plan_contracts") as batch:
        batch.add_column(sa.Column("source_journey_plan_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_comment_plan_source_journey",
            "cross_adapter_source_journey_plan_revisions",
            ["source_journey_plan_id"], ["id"], ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("channel_comment_plan_contracts") as batch:
        batch.drop_constraint("fk_comment_plan_source_journey", type_="foreignkey")
        batch.drop_column("source_journey_plan_id")
