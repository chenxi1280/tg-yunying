"""Stable album participant child sets; no conversion of existing remote facts."""
from alembic import op
import sqlalchemy as sa

revision = "0221_album_reaction"
down_revision = "0220_channel_source_intake"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("album_reaction_participations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("lifecycle_epoch", sa.Integer(), nullable=False),
        sa.Column("task_day_ledger_id", sa.String(36), sa.ForeignKey("task_day_ledgers.id"), nullable=False),
        sa.Column("channel_target_id", sa.Integer(), sa.ForeignKey("operation_targets.id"), nullable=False),
        sa.Column("target_peer_id", sa.String(160), nullable=False),
        sa.Column("album_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("source_revision_hash", sa.String(64), nullable=False),
        sa.Column("children", sa.JSON(), nullable=False),
        sa.Column("child_count", sa.Integer(), nullable=False),
        sa.Column("child_count_reason", sa.String(80), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "lifecycle_epoch", "channel_target_id", "album_id", "account_id", name="uq_album_reaction_participant"))


def downgrade():
    op.drop_table("album_reaction_participations")
