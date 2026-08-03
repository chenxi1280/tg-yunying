"""Add indexes required for set-based physical task deletion."""

from alembic import op


revision = "0138_physical_delete_hot_indexes"
down_revision = "0137_fulfillment_v2"
branch_labels = None
depends_on = None


INDEXES = (
    ("ix_ai_scope_takeover_item_action_id", "ai_content_scope_takeover_items", "action_id"),
    ("ix_gateway_request_evidence_action_id", "gateway_request_evidence_journals", "action_id"),
    ("ix_gateway_request_evidence_attempt_id", "gateway_request_evidence_journals", "execution_attempt_id"),
    ("ix_comment_fulfillment_current_action", "comment_fulfillment_obligations", "current_action_id"),
    ("ix_reaction_fulfillment_current_action", "reaction_fulfillment_obligations", "current_action_id"),
    ("ix_view_fulfillment_current_action", "view_fulfillment_obligations", "current_action_id"),
    ("ix_search_click_fulfillment_source_action", "search_click_fulfillment_obligations", "source_action_id"),
    ("ix_search_click_fulfillment_attempt", "search_click_fulfillment_obligations", "execution_attempt_id"),
    ("ix_content_mix_slot_current_action", "content_mix_cycle_slots", "current_action_id"),
    ("ix_content_mix_obligation_assigned_action", "content_mix_obligations", "assigned_action_id"),
)


def upgrade() -> None:
    for name, table, column in INDEXES:
        op.create_index(name, table, [column], if_not_exists=True)


def downgrade() -> None:
    for name, table, _column in reversed(INDEXES):
        op.drop_index(name, table_name=table, if_exists=True)
