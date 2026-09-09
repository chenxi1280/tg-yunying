"""Retain emergency history across proved-unsent obligation materializations."""
from alembic import op
import sqlalchemy as sa

revision = "0231_ai_group_emergency_history"
down_revision = "0230_ai_group_emergency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_ai_group_emergency_quantity", "ai_group_emergency_selections", type_="unique")
    op.create_unique_constraint("uq_ai_group_emergency_quantity_version", "ai_group_emergency_selections",
                                ["primary_quantity_slot_id", "materialization_version"])


def downgrade() -> None:
    duplicate = op.get_bind().scalar(sa.text(
        "SELECT count(*) FROM (SELECT primary_quantity_slot_id FROM ai_group_emergency_selections "
        "GROUP BY primary_quantity_slot_id HAVING count(*) > 1) history"))
    if duplicate:
        raise RuntimeError("Emergency materialization history must be preserved; use a forward fix")
    op.drop_constraint("uq_ai_group_emergency_quantity_version", "ai_group_emergency_selections", type_="unique")
    op.create_unique_constraint("uq_ai_group_emergency_quantity", "ai_group_emergency_selections",
                                ["primary_quantity_slot_id"])
