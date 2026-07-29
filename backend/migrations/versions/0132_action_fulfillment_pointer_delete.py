"""Release Action fulfillment pointers when their derived slots are deleted.

Revision ID: 0132_action_pointer_delete
Revises: 0131_search_click_runtime
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op


revision = "0132_action_pointer_delete"
down_revision = "0131_search_click_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "fk_actions_primary_quantity_slot",
        "actions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_actions_primary_quantity_slot",
        "actions",
        "task_group_daily_message_slots",
        ["primary_quantity_slot_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint(
        "fk_actions_content_mix_cycle_slot",
        "actions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_actions_content_mix_cycle_slot",
        "actions",
        "content_mix_cycle_slots",
        ["content_mix_cycle_slot_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    raise RuntimeError("0132 Action fulfillment pointer migration is forward-only")
