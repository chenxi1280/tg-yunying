from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class CloneSourceStreamState(Base):
    """
    任务源群 stream 水位与消费状态（start_from_now 边界与 difference 推进）。
    """
    __tablename__ = "clone_source_stream_states"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", "task_lifecycle_epoch", name="uq_clone_stream_task_epoch"),
        Index("ix_clone_stream_state_lease", "owner_id", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    source_peer_type: Mapped[str] = mapped_column(String(32))
    source_peer_id: Mapped[str] = mapped_column(String(120))
    listener_account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    authorization_id: Mapped[int] = mapped_column(ForeignKey("tg_account_authorizations.id", ondelete="CASCADE"))
    start_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    start_pts: Mapped[int] = mapped_column(BigInteger, default=0)
    authorization_update_state_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_consumed_ingress_order_no: Mapped[int] = mapped_column(BigInteger, default=0)
    channel_pts: Mapped[int] = mapped_column(BigInteger, default=0)
    difference_cursor: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(32), default="initializing")  # initializing, catching_up, live, gap, blocked, stopped
    owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_fencing_epoch: Mapped[int] = mapped_column(Integer, default=1)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_applied_event_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_applied_stream_order_no: Mapped[int] = mapped_column(BigInteger, default=0)
    last_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class CloneSourceEvent(Base):
    """
    来源事件事实表（固化 PTS、复合身份、Entities、Grouped ID 与连续 stream_order_no）。
    """
    __tablename__ = "clone_source_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "task_id",
            "task_lifecycle_epoch",
            "source_peer_type",
            "source_peer_id",
            "event_identity_hash",
            name="uq_clone_source_event_identity",
        ),
        UniqueConstraint(
            "tenant_id",
            "task_id",
            "task_lifecycle_epoch",
            "stream_order_no",
            name="uq_clone_source_stream_order",
        ),
        Index("ix_clone_source_event_grouped", "task_id", "grouped_id"),
        Index("ix_clone_source_event_msg", "task_id", "source_message_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    source_peer_type: Mapped[str] = mapped_column(String(32))
    source_peer_id: Mapped[str] = mapped_column(String(120))
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(32))  # message_new, message_edit, message_delete, message_pin, topic_create, topic_edit, topic_delete
    ingress_update_identity_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_identity_hash: Mapped[str] = mapped_column(String(64))
    source_pts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_pts_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    authorization_ingress_order_no: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    normalized_item_index: Mapped[int] = mapped_column(Integer, default=0)
    apply_order_key: Mapped[str] = mapped_column(String(160))
    stream_order_no: Mapped[int] = mapped_column(BigInteger)
    message_revision: Mapped[int] = mapped_column(Integer, default=1)
    sender_peer_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sender_peer_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_top_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    grouped_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content: Mapped[str] = mapped_column(Text, default="")
    entities: Mapped[list] = mapped_column(JSON, default=list)
    poll_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    content_fingerprint: Mapped[str] = mapped_column(String(64))
    protected_content: Mapped[bool] = mapped_column(Boolean, default=False)
    config_revision: Mapped[int] = mapped_column(Integer, default=1)
    sanitization_revision: Mapped[int] = mapped_column(Integer, default=1)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CloneTargetRouteSnapshot(Base):
    """
    不可变 Route 绑定快照（防串群防串台）。
    """
    __tablename__ = "clone_target_route_snapshots"
    __table_args__ = (
        UniqueConstraint("task_id", "epoch", "route_binding_version", name="uq_clone_route_snapshot_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    route_binding_version: Mapped[int] = mapped_column(Integer, default=1)
    config_revision: Mapped[int] = mapped_column(Integer, default=1)
    source_internal_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_operation_target_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_peer_type: Mapped[str] = mapped_column(String(32))
    source_peer_id: Mapped[str] = mapped_column(String(120))
    target_internal_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_operation_target_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    target_peer_type: Mapped[str] = mapped_column(String(32))
    target_peer_id: Mapped[str] = mapped_column(String(120))
    reply_target_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    target_top_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    route_binding_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CloneTargetExecutionSnapshot(Base):
    """
    不可变 Execution 绑定快照（区分 sender-role 与 control-role）。
    """
    __tablename__ = "clone_target_execution_snapshots"
    __table_args__ = (
        UniqueConstraint("route_snapshot_id", "execution_binding_version", name="uq_clone_execution_snapshot_ver"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    route_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("clone_target_route_snapshots.id", ondelete="CASCADE")
    )
    execution_binding_version: Mapped[int] = mapped_column(Integer, default=1)
    execution_role: Mapped[str] = mapped_column(String(32), default="sender")  # sender, target_control
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    authorization_id: Mapped[int] = mapped_column(ForeignKey("tg_account_authorizations.id", ondelete="CASCADE"))
    session_generation: Mapped[int] = mapped_column(Integer, default=1)
    account_target_relation_version: Mapped[int] = mapped_column(Integer, default=1)
    sender_binding_history_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sender_binding_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    execution_binding_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CloneAccountSlot(Base):
    """
    任务号池可锁权威槽位行（即使账号当前空闲也存在）。
    """
    __tablename__ = "clone_account_slots"
    __table_args__ = (
        UniqueConstraint("task_id", "account_id", name="uq_clone_account_slot_task_account"),
        Index("ix_clone_account_slot_state", "task_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    authorization_id: Mapped[int] = mapped_column(ForeignKey("tg_account_authorizations.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String(32), default="available")  # available, reserved, active, cooling, disabled
    projected_transport_blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_fencing_epoch: Mapped[int] = mapped_column(Integer, default=1)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class CloneSenderBindingHistory(Base):
    """
    发言人绑定版本与历史事实表（状态机流转与审计日志）。
    """
    __tablename__ = "clone_sender_binding_history"
    __table_args__ = (
        Index("ix_clone_sender_binding_task_status", "task_id", "status"),
        Index(
            "uq_clone_active_sender_slot",
            "task_id",
            "task_lifecycle_epoch",
            "source_sender_peer_type",
            "source_sender_peer_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'guarded', 'eligible')"),
            sqlite_where=text("status IN ('active', 'guarded', 'eligible')"),
        ),
        Index(
            "uq_clone_active_account_slot",
            "task_id",
            "task_lifecycle_epoch",
            "assigned_account_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'guarded', 'eligible')"),
            sqlite_where=text("status IN ('active', 'guarded', 'eligible')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    binding_version: Mapped[int] = mapped_column(Integer, default=1)
    source_sender_peer_type: Mapped[str] = mapped_column(String(32))
    source_sender_peer_id: Mapped[str] = mapped_column(String(120))
    source_sender_name: Mapped[str] = mapped_column(String(160), default="")
    assigned_account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    account_slot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")  # active, guarded, eligible, expired, disabled
    is_vip: Mapped[bool] = mapped_column(Boolean, default=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_spoken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_reassigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    reassignment_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CloneAlbumManifest(Base):
    """
    相册 Manifest 聚合与静默窗口事实。
    """
    __tablename__ = "clone_album_manifests"
    __table_args__ = (
        UniqueConstraint("task_id", "epoch", "grouped_id", name="uq_clone_album_manifest_grouped"),
        Index("ix_clone_album_manifest_state", "task_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    grouped_id: Mapped[str] = mapped_column(String(64))
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    quiet_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    max_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    items_total: Mapped[int] = mapped_column(Integer, default=0)
    collection_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    state: Mapped[str] = mapped_column(String(32), default="collecting")  # collecting, verifying_source, ready, incomplete_timeout, failed_dropped, ready_partial_degraded, action_bound, succeeded, failed
    frozen_policy: Mapped[str] = mapped_column(String(32), default="drop_incomplete")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CloneAlbumItem(Base):
    """
    相册分片项目事实。
    """
    __tablename__ = "clone_album_items"
    __table_args__ = (
        UniqueConstraint("manifest_id", "part_index", name="uq_clone_album_item_part"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    manifest_id: Mapped[str] = mapped_column(
        ForeignKey("clone_album_manifests.id", ondelete="CASCADE")
    )
    source_event_id: Mapped[str] = mapped_column(
        ForeignKey("clone_source_events.id", ondelete="CASCADE")
    )
    part_index: Mapped[int] = mapped_column(Integer)
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    media_type: Mapped[str] = mapped_column(String(32))
    media_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    item_fingerprint: Mapped[str] = mapped_column(String(64))
    acquisition_state: Mapped[str] = mapped_column(String(32), default="acquired")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CloneTopicMap(Base):
    """
    话题映射与 Lazy Bootstrap 状态。
    """
    __tablename__ = "clone_topic_maps"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "epoch",
            "source_peer_type",
            "source_peer_id",
            "source_top_message_id",
            name="uq_clone_topic_map_source",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    source_peer_type: Mapped[str] = mapped_column(String(32))
    source_peer_id: Mapped[str] = mapped_column(String(120))
    source_top_message_id: Mapped[int] = mapped_column(BigInteger)
    target_top_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    topic_title_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    topic_icon_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    state: Mapped[str] = mapped_column(String(32), default="placeholder")  # placeholder, creating, ready, unknown, blocked, failed, deleted
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TelegramGatewayMutationIdentity(Base):
    """
    Gateway 前不可变 Mutation Identity 权威（跨 Session 永久唯一 random_id 墓碑）。
    """
    __tablename__ = "telegram_gateway_mutation_identities"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "epoch",
            "obligation_id",
            "materialization_version",
            "mutation_kind",
            "part_index",
            name="uq_gateway_mutation_identity_scope",
        ),
        Index(
            "uq_gateway_mutation_random_id_peer",
            "tenant_id",
            "telegram_account_peer_id",
            "target_peer_type",
            "target_peer_id",
            "random_id",
            unique=True,
            postgresql_where=text("random_id IS NOT NULL"),
            sqlite_where=text("random_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    obligation_id: Mapped[str] = mapped_column(String(255))
    materialization_version: Mapped[int] = mapped_column(Integer, default=1)
    mutation_kind: Mapped[str] = mapped_column(String(40))  # sendMessage, sendMedia, sendMultiMedia, editMessage, deleteMessages, pinMessage, createForumTopic
    part_index: Mapped[int] = mapped_column(Integer, default=0)
    execution_role: Mapped[str] = mapped_column(String(32), default="sender")  # sender, target_control
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    telegram_account_peer_id: Mapped[str] = mapped_column(String(120))
    authorization_id: Mapped[int] = mapped_column(ForeignKey("tg_account_authorizations.id", ondelete="CASCADE"))
    session_generation: Mapped[int] = mapped_column(Integer, default=1)
    target_peer_type: Mapped[str] = mapped_column(String(32))
    target_peer_id: Mapped[str] = mapped_column(String(120))
    random_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    derivation_version: Mapped[int] = mapped_column(Integer, default=1)
    collision_nonce: Mapped[int] = mapped_column(Integer, default=0)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="allocated")  # allocated, attempt_bound, unknown, closed
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

