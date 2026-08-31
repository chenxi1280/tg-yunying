"""Add channel comment fallback policy, pool, cursor, and selections.

Revision ID: 0185_channel_comment_fallback
Revises: 0184_ai_group_content_alloc
"""

from alembic import op
import sqlalchemy as sa


revision = "0185_channel_comment_fallback"
down_revision = "0184_ai_group_content_alloc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_policy_table()
    _create_pool_table()
    _create_cursor_table()
    _create_selection_table()


def _create_policy_table() -> None:
    op.create_table(
        "comment_fallback_policy_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("task_config_revision", sa.Integer(), nullable=False),
        sa.Column("fallback_policy_version", sa.String(length=40), nullable=False),
        sa.Column("unicode_allowlist_version", sa.String(length=40), nullable=False),
        sa.Column("unicode_allowlist_hash", sa.String(length=64), nullable=False),
        sa.Column("unicode_enabled", sa.Boolean(), nullable=False),
        sa.Column("image_meme_enabled", sa.Boolean(), nullable=False),
        sa.Column("image_meme_material_group_id", sa.Integer(), nullable=True),
        sa.Column("unicode_weight_bps", sa.Integer(), nullable=False),
        sa.Column("image_meme_weight_bps", sa.Integer(), nullable=False),
        sa.Column("allow_image_reselection_before_gateway", sa.Boolean(), nullable=False),
        sa.Column("allow_cross_kind_fallback_to_unicode", sa.Boolean(), nullable=False),
        sa.Column("material_contract_version", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["image_meme_material_group_id"], ["material_groups.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "task_config_revision", name="uq_comment_fallback_policy_task_revision"),
    )


def _create_pool_table() -> None:
    op.create_table(
        "channel_comment_fallback_pool_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("channel_message_id", sa.Integer(), nullable=False),
        sa.Column("comment_plan_revision", sa.Integer(), nullable=False),
        sa.Column("content_mix_contract_id", sa.String(length=36), nullable=False),
        sa.Column("fallback_policy_snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("image_meme_assets", sa.JSON(), nullable=False),
        sa.Column("image_meme_asset_pool_hash", sa.String(length=64), nullable=False),
        sa.Column("pool_state", sa.String(length=48), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["channel_message_id"], ["channel_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["content_mix_contract_id"], ["content_mix_contracts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["fallback_policy_snapshot_id"], ["comment_fallback_policy_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_mix_contract_id", name="uq_channel_comment_fallback_pool_plan"),
    )


def _create_cursor_table() -> None:
    op.create_table(
        "fallback_shuffle_bag_cursors",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("content_mix_contract_id", sa.String(length=36), nullable=False),
        sa.Column("fallback_content_kind", sa.String(length=32), nullable=False),
        sa.Column("bag_seed", sa.String(length=128), nullable=False),
        sa.Column("bag_order_hash", sa.String(length=64), nullable=False),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column("next_rank", sa.Integer(), nullable=False),
        sa.Column("cursor_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["content_mix_contract_id"], ["content_mix_contracts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_mix_contract_id", "fallback_content_kind", name="uq_fallback_shuffle_cursor_plan_kind"),
    )


def _create_selection_table() -> None:
    op.create_table(
        "comment_fallback_selections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("content_mix_contract_id", sa.String(length=36), nullable=False),
        sa.Column("target_ordinal", sa.Integer(), nullable=False),
        sa.Column("assignment_version", sa.Integer(), nullable=False),
        sa.Column("selection_attempt", sa.Integer(), nullable=False),
        sa.Column("fallback_kind", sa.String(length=24), nullable=False),
        sa.Column("fallback_content_kind", sa.String(length=32), nullable=False),
        sa.Column("fallback_pool_snapshot_id", sa.String(length=36), nullable=True),
        sa.Column("selection_seed", sa.String(length=128), nullable=False),
        sa.Column("selection_cycle", sa.Integer(), nullable=False),
        sa.Column("selection_rank", sa.Integer(), nullable=False),
        sa.Column("unicode_emoji", sa.String(length=16), nullable=True),
        sa.Column("material_id", sa.Integer(), nullable=True),
        sa.Column("asset_version_id", sa.Integer(), nullable=True),
        sa.Column("asset_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("tg_ref_version_id", sa.Integer(), nullable=True),
        sa.Column("tg_cache_peer_id", sa.String(length=160), nullable=False),
        sa.Column("tg_cache_message_id", sa.String(length=160), nullable=False),
        sa.Column("asset_pool_hash", sa.String(length=64), nullable=False),
        sa.Column("fallback_reason", sa.String(length=255), nullable=False),
        sa.Column("selection_state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["content_mix_contract_id"], ["content_mix_contracts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["fallback_pool_snapshot_id"], ["channel_comment_fallback_pool_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["material_id"], ["materials.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("content_mix_contract_id", "target_ordinal", "assignment_version", "selection_attempt", name="uq_comment_fallback_selection_attempt"),
    )


def downgrade() -> None:
    op.drop_table("comment_fallback_selections")
    op.drop_table("fallback_shuffle_bag_cursors")
    op.drop_table("channel_comment_fallback_pool_snapshots")
    op.drop_table("comment_fallback_policy_snapshots")
