"""Add context_turns author_peer_id, reaction intent, and negative outcome circuit tables."""
from alembic import op
import sqlalchemy as sa

revision = "0223_burst_negative_outcome"
down_revision = "0222_channel_source_cursor"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add author_peer_id to context_turns
    with op.batch_alter_table("context_turns") as batch_op:
        batch_op.add_column(
            sa.Column("author_peer_id", sa.String(120), nullable=False, server_default="")
        )
        batch_op.create_index(
            "ix_context_turns_author", ["tenant_id", "canonical_peer_id", "author_peer_id"]
        )

    # 2. reaction_intent_policy_revisions
    op.create_table(
        "reaction_intent_policy_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(32), nullable=False, server_default="active"),
        sa.Column("intent_mappings", sa.JSON(), nullable=False),
        sa.Column("safe_intents", sa.JSON(), nullable=False),
        sa.Column("negative_keywords", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "revision", name="uq_reaction_intent_policy_revision"),
    )

    # 3. source_reaction_intent_decisions
    op.create_table(
        "source_reaction_intent_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_revision", sa.String(64), nullable=False),
        sa.Column("policy_revision_id", sa.String(36), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("detected_intents", sa.JSON(), nullable=False),
        sa.Column("has_negative_keywords", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allowed_reactions", sa.JSON(), nullable=False),
        sa.Column("configured_reactions", sa.JSON(), nullable=False),
        sa.Column("candidate_reactions", sa.JSON(), nullable=False),
        sa.Column("chosen_reaction", sa.String(32), nullable=False, server_default=""),
        sa.Column("decision", sa.String(40), nullable=False, server_default="confirmed"),
        sa.Column("reason", sa.String(80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "account_id", "source_revision", "policy_revision_id", name="uq_source_reaction_intent_decision"),
    )
    op.create_index(
        "ix_reaction_intent_task_source",
        "source_reaction_intent_decisions",
        ["task_id", "source_revision"],
    )

    # 4. negative_outcome_policy_revisions
    op.create_table(
        "negative_outcome_policy_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(32), nullable=False, server_default="active"),
        sa.Column("event_types", sa.JSON(), nullable=False),
        sa.Column("proactive_throttled_threshold", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("response_restricted_threshold", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("quarantine_threshold", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("manual_review_threshold", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("minimum_hold_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("recovery_window_seconds", sa.Integer(), nullable=False, server_default="1800"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "revision", name="uq_negative_outcome_policy_revision"),
    )

    # 5. negative_outcome_circuit_states
    op.create_table(
        "negative_outcome_circuit_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("peer_id", sa.String(120), nullable=False, server_default=""),
        sa.Column("route", sa.String(40), nullable=False, server_default=""),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=True),
        sa.Column("level", sa.String(32), nullable=False, server_default="normal"),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("eligible_exit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(80), nullable=False, server_default=""),
        sa.Column("policy_revision_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "route", "peer_id", "account_id", name="uq_negative_outcome_circuit_scope"),
    )
    op.create_index(
        "ix_negative_outcome_circuit_level",
        "negative_outcome_circuit_states",
        ["tenant_id", "level"],
    )


def downgrade():
    op.drop_table("negative_outcome_circuit_states")
    op.drop_table("negative_outcome_policy_revisions")
    op.drop_table("source_reaction_intent_decisions")
    op.drop_table("reaction_intent_policy_revisions")
    with op.batch_alter_table("context_turns") as batch_op:
        batch_op.drop_index("ix_context_turns_author")
        batch_op.drop_column("author_peer_id")
