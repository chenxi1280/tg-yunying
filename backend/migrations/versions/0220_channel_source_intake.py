"""Freeze channel intake boundaries and preserve source-kind evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0220_channel_source_intake"
down_revision = "0219_lightweight_timing"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("channel_messages", sa.Column("grouped_id", sa.String(64), nullable=False, server_default=""))
    op.add_column("channel_messages", sa.Column("source_metadata", sa.JSON(), nullable=False, server_default="{}"))
    op.create_table("channel_task_intakes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("lifecycle_epoch", sa.Integer(), nullable=False),
        sa.Column("channel_target_id", sa.Integer(), sa.ForeignKey("operation_targets.id"), nullable=False),
        sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("initial_source_keys", sa.JSON(), nullable=False),
        sa.Column("historical_limit", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "lifecycle_epoch", "channel_target_id", name="uq_channel_task_intake"))
    op.create_table("channel_source_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("intake_id", sa.String(36), sa.ForeignKey("channel_task_intakes.id"), nullable=False),
        sa.Column("channel_message_id", sa.Integer(), sa.ForeignKey("channel_messages.id"), nullable=False),
        sa.Column("source_key", sa.String(100), nullable=False),
        sa.Column("decision", sa.String(48), nullable=False),
        sa.Column("reason", sa.String(80), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("intake_id", "channel_message_id", name="uq_channel_source_decision"))


def downgrade():
    op.drop_table("channel_source_decisions")
    op.drop_table("channel_task_intakes")
    op.drop_column("channel_messages", "source_metadata")
    op.drop_column("channel_messages", "grouped_id")
