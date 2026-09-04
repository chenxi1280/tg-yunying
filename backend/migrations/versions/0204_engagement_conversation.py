"""Add canonical conversation turn and interaction opportunity contracts.

Revision ID: 0204_engagement_conversation
Revises: 0203_task_target_scope_claims
"""

from alembic import op
import sqlalchemy as sa


revision = "0204_engagement_conversation"
down_revision = "0203_task_target_scope_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_conversation_events()
    _create_context_turns()
    _create_interaction_opportunities()
    _create_conversation_turn_claims()
    _create_stage_wake_outbox()


def _create_conversation_events() -> None:
    op.create_table(
        "conversation_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("surface", sa.String(40), nullable=False),
        sa.Column("canonical_peer_id", sa.String(120), nullable=False),
        sa.Column("target_group_id", sa.Integer(), sa.ForeignKey("tg_groups.id", ondelete="CASCADE"), nullable=True),
        sa.Column("remote_message_id", sa.String(160), nullable=False),
        sa.Column("event_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("author_class", sa.String(40), nullable=False),
        sa.Column("author_peer_id", sa.String(120), nullable=False, server_default=""),
        sa.Column("author_name", sa.String(160), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("modality", sa.String(40), nullable=False, server_default="text"),
        sa.Column("source_context_message_id", sa.Integer(), sa.ForeignKey("group_context_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "surface", "canonical_peer_id", "remote_message_id", "event_revision", name="uq_conversation_event_remote_revision"),
        sa.UniqueConstraint("source_context_message_id", name="uq_conversation_event_context_source"),
    )
    op.create_index("ix_conversation_event_peer_current", "conversation_events", ["tenant_id", "surface", "canonical_peer_id", "is_current", "sent_at"])


def _create_context_turns() -> None:
    op.create_table(
        "context_turns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("surface", sa.String(40), nullable=False),
        sa.Column("canonical_peer_id", sa.String(120), nullable=False),
        sa.Column("target_group_id", sa.Integer(), sa.ForeignKey("tg_groups.id", ondelete="CASCADE"), nullable=True),
        sa.Column("turn_family_key", sa.String(220), nullable=False),
        sa.Column("anchor_event_id", sa.String(36), sa.ForeignKey("conversation_events.id"), nullable=False),
        sa.Column("event_ids", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="assembling"),
        sa.Column("first_event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("topic_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "surface", "canonical_peer_id", "turn_family_key", name="uq_context_turn_family"),
    )
    op.create_index("ix_context_turn_due", "context_turns", ["tenant_id", "surface", "canonical_peer_id", "state", "closed_at"])


def _create_interaction_opportunities() -> None:
    op.create_table(
        "interaction_opportunities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False),
        sa.Column("context_turn_id", sa.String(36), sa.ForeignKey("context_turns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("anchor_event_id", sa.String(36), sa.ForeignKey("conversation_events.id"), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="admitted"),
        sa.Column("relation_kind", sa.String(40), nullable=False, server_default="native_reply_external_human"),
        sa.Column("natural_not_before_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("freshness_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "task_lifecycle_epoch", "context_turn_id", name="uq_interaction_opportunity_task_turn"),
    )
    op.create_index("ix_interaction_opportunity_task_state", "interaction_opportunities", ["task_id", "state", "freshness_deadline_at"])


def _create_conversation_turn_claims() -> None:
    op.create_table(
        "conversation_turn_claims",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("context_turn_id", sa.String(36), sa.ForeignKey("context_turns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("interaction_opportunity_id", sa.String(36), sa.ForeignKey("interaction_opportunities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=True),
        sa.Column("action_id", sa.String(36), sa.ForeignKey("actions.id"), nullable=True),
        sa.Column("state", sa.String(32), nullable=False, server_default="claimed"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settlement_reason", sa.String(80), nullable=False, server_default=""),
        sa.UniqueConstraint("context_turn_id", name="uq_conversation_turn_claim_turn"),
        sa.UniqueConstraint("interaction_opportunity_id", name="uq_conversation_turn_claim_opportunity"),
    )
    op.create_index("ix_conversation_turn_claim_task_state", "conversation_turn_claims", ["task_id", "state"])


def _create_stage_wake_outbox() -> None:
    op.create_table(
        "stage_wake_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("aggregate_type", sa.String(40), nullable=False),
        sa.Column("aggregate_id", sa.String(80), nullable=False),
        sa.Column("aggregate_revision", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(48), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("aggregate_type", "aggregate_id", "aggregate_revision", "stage", name="uq_stage_wake_aggregate_revision"),
    )
    op.create_index("ix_stage_wake_due", "stage_wake_outbox", ["state", "available_at", "created_at"])


def downgrade() -> None:
    for table, indexes in (
        ("stage_wake_outbox", ("ix_stage_wake_due",)),
        ("conversation_turn_claims", ("ix_conversation_turn_claim_task_state",)),
        ("interaction_opportunities", ("ix_interaction_opportunity_task_state",)),
        ("context_turns", ("ix_context_turn_due",)),
        ("conversation_events", ("ix_conversation_event_peer_current",)),
    ):
        for index in indexes:
            op.drop_index(index, table_name=table)
        op.drop_table(table)
