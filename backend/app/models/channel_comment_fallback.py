from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class CommentFallbackPolicySnapshot(Base):
    __tablename__ = "comment_fallback_policy_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "task_config_revision",
            name="uq_comment_fallback_policy_task_revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_config_revision: Mapped[int] = mapped_column(Integer)
    fallback_policy_version: Mapped[str] = mapped_column(String(40))
    unicode_allowlist_version: Mapped[str] = mapped_column(String(40))
    unicode_allowlist_hash: Mapped[str] = mapped_column(String(64))
    unicode_enabled: Mapped[bool] = mapped_column(Boolean)
    image_meme_enabled: Mapped[bool] = mapped_column(Boolean)
    image_meme_material_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("material_groups.id"), nullable=True,
    )
    unicode_weight_bps: Mapped[int] = mapped_column(Integer)
    image_meme_weight_bps: Mapped[int] = mapped_column(Integer)
    allow_image_reselection_before_gateway: Mapped[bool] = mapped_column(Boolean)
    allow_cross_kind_fallback_to_unicode: Mapped[bool] = mapped_column(Boolean)
    material_contract_version: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelCommentFallbackPoolSnapshot(Base):
    __tablename__ = "channel_comment_fallback_pool_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "content_mix_contract_id",
            name="uq_channel_comment_fallback_pool_plan",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    channel_message_id: Mapped[int] = mapped_column(
        ForeignKey("channel_messages.id", ondelete="CASCADE"),
    )
    comment_plan_revision: Mapped[int] = mapped_column(Integer)
    content_mix_contract_id: Mapped[str] = mapped_column(
        ForeignKey("content_mix_contracts.id", ondelete="CASCADE"),
    )
    fallback_policy_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("comment_fallback_policy_snapshots.id", ondelete="CASCADE"),
    )
    image_meme_assets: Mapped[list] = mapped_column(JSON, default=list)
    image_meme_asset_pool_hash: Mapped[str] = mapped_column(String(64))
    pool_state: Mapped[str] = mapped_column(String(48), default="ready")
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class FallbackShuffleBagCursor(Base):
    __tablename__ = "fallback_shuffle_bag_cursors"
    __table_args__ = (
        UniqueConstraint(
            "content_mix_contract_id", "fallback_content_kind",
            name="uq_fallback_shuffle_cursor_plan_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    content_mix_contract_id: Mapped[str] = mapped_column(
        ForeignKey("content_mix_contracts.id", ondelete="CASCADE"),
    )
    fallback_content_kind: Mapped[str] = mapped_column(String(32))
    bag_seed: Mapped[str] = mapped_column(String(128))
    bag_order_hash: Mapped[str] = mapped_column(String(64))
    cycle: Mapped[int] = mapped_column(Integer, default=0)
    next_rank: Mapped[int] = mapped_column(Integer, default=0)
    cursor_version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class CommentFallbackSelection(Base):
    __tablename__ = "comment_fallback_selections"
    __table_args__ = (
        UniqueConstraint(
            "content_mix_contract_id", "target_ordinal", "assignment_version",
            "selection_attempt", name="uq_comment_fallback_selection_attempt",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    content_mix_contract_id: Mapped[str] = mapped_column(
        ForeignKey("content_mix_contracts.id", ondelete="CASCADE"),
    )
    target_ordinal: Mapped[int] = mapped_column(Integer)
    assignment_version: Mapped[int] = mapped_column(Integer, default=1)
    selection_attempt: Mapped[int] = mapped_column(Integer, default=1)
    fallback_kind: Mapped[str] = mapped_column(String(24))
    fallback_content_kind: Mapped[str] = mapped_column(String(32))
    fallback_pool_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("channel_comment_fallback_pool_snapshots.id", ondelete="CASCADE"),
        nullable=True,
    )
    selection_seed: Mapped[str] = mapped_column(String(128))
    selection_cycle: Mapped[int] = mapped_column(Integer)
    selection_rank: Mapped[int] = mapped_column(Integer)
    unicode_emoji: Mapped[str | None] = mapped_column(String(16), nullable=True)
    material_id: Mapped[int | None] = mapped_column(ForeignKey("materials.id"), nullable=True)
    asset_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    asset_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tg_ref_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tg_cache_peer_id: Mapped[str] = mapped_column(String(160), default="")
    tg_cache_message_id: Mapped[str] = mapped_column(String(160), default="")
    asset_pool_hash: Mapped[str] = mapped_column(String(64), default="")
    fallback_reason: Mapped[str] = mapped_column(String(255), default="")
    selection_state: Mapped[str] = mapped_column(String(32), default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "ChannelCommentFallbackPoolSnapshot",
    "CommentFallbackPolicySnapshot",
    "CommentFallbackSelection",
    "FallbackShuffleBagCursor",
]
