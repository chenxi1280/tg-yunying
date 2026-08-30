from __future__ import annotations

import hashlib
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models.enums import now
from app.models.fulfillment_v2 import FulfillmentObligationProjection, FulfillmentRemoteFact
from app.models.group_clone import (

    CloneAccountSlot,
    CloneAlbumItem,
    CloneAlbumManifest,
    CloneCutoverExclusion,
    CloneDeliveryObligation,
    CloneManualReviewDecision,
    CloneMessagePart,
    CloneSenderBindingHistory,
    CloneSequencerHeadCase,
    CloneSourceEvent,
    CloneSourceStreamState,
    CloneTargetExecutionSnapshot,
    CloneTargetRouteSnapshot,
    CloneTopicMap,
    TelegramGatewayMutationIdentity,
)
from app.models.task_center import Action, Task
from app.models.telegram_authorities import (
    TelegramAuthorizationTransportState,
    TelegramGroupMutationAuthority,
    TelegramGroupMutationAuthorityHolder,
)
from app.models.telegram_updates import (
    TelegramAuthorizationUpdateDelivery,
    TelegramAuthorizationUpdateEvent,
    TelegramAuthorizationUpdateState,
    TelegramAuthorizationUpdateSubscription,
)
from app.services.task_center.group_mutation_authority import (
    check_and_claim_exclusive_authority,
    compute_route_hash,
    verify_gateway_admission,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. 确定性 random_id 生成算法 (§10.1)
# ---------------------------------------------------------------------------
def derive_deterministic_random_id(
    contract: str,
    tenant_id: int,
    task_id: str,
    epoch: int,
    obligation_id: str,
    mutation_kind: str,
    part_index: int,
    derivation_version: int = 1,
    collision_nonce: int = 0,
) -> int:
    """
    非零 signed 64-bit 确定性 hash 派生。
    """
    raw = (
        f"{contract}:{tenant_id}:{task_id}:{epoch}:{obligation_id}:"
        f"{mutation_kind}:{part_index}:{derivation_version}:{collision_nonce}"
    )
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    val = int.from_bytes(digest[:8], byteorder="big", signed=True)
    if val == 0:
        val = 1
    return val


# ---------------------------------------------------------------------------
# 2. 发言人绑定管理器与三道安全闸门 (§7.1, §7.2)
# ---------------------------------------------------------------------------
class CloneSenderBindingManager:
    @staticmethod
    def get_or_assign_sender_binding(
        session: Session,
        task: Task,
        source_sender_peer_type: str,
        source_sender_peer_id: str,
        source_sender_name: str,
        reply_to_sender_peer_id: Optional[str] = None,
        is_vip: bool = False,
    ) -> Tuple[Optional[CloneSenderBindingHistory], str]:
        """
        获取或原子分配马甲账号。包含 Gate 1 (Cross-talk), Gate 2 (Reply self-collision), Gate 3 (Min tenure)。
        """
        type_config = task.type_config or {}
        sender_pool_config = type_config.get("sender_pool", {})
        allowed_account_ids = sender_pool_config.get("account_ids", [])
        cooldown_sec = sender_pool_config.get("reassignment_cooldown_seconds", 300)
        current_time = datetime.now(timezone.utc)

        # 1. 查询当前源发言人是否已有有效 binding (active / guarded / eligible)
        stmt = (
            select(CloneSenderBindingHistory)
            .where(
                CloneSenderBindingHistory.task_id == task.id,
                CloneSenderBindingHistory.task_lifecycle_epoch == task.task_lifecycle_epoch,
                CloneSenderBindingHistory.source_sender_peer_type == source_sender_peer_type,
                CloneSenderBindingHistory.source_sender_peer_id == source_sender_peer_id,
                CloneSenderBindingHistory.status.in_(["active", "guarded", "eligible"]),
            )
            .with_for_update()
        )
        existing = session.execute(stmt).scalar_one_or_none()

        if existing:
            # Gate 2: 防自己回复自己（同一消息链内父子不能为同一马甲号）
            if reply_to_sender_peer_id and reply_to_sender_peer_id != source_sender_peer_id:
                parent_binding = session.execute(
                    select(CloneSenderBindingHistory).where(
                        CloneSenderBindingHistory.task_id == task.id,
                        CloneSenderBindingHistory.task_lifecycle_epoch == task.task_lifecycle_epoch,
                        CloneSenderBindingHistory.source_sender_peer_id == reply_to_sender_peer_id,
                        CloneSenderBindingHistory.status.in_(["active", "guarded", "eligible"]),
                    )
                ).scalar_one_or_none()
                if parent_binding and parent_binding.assigned_account_id == existing.assigned_account_id:
                    return None, f"Gate 2 拦截：源发言人 {source_sender_peer_id} 与回复父发言人 {reply_to_sender_peer_id} 映射到同一马甲号 {existing.assigned_account_id}"

            existing.last_spoken_at = current_time
            if is_vip:
                existing.is_vip = True
            session.flush()
            return existing, ""

        # 2. 需要分配新账号：从 allowed_account_ids 中查找空闲或可回收的 slot
        active_bindings_stmt = select(CloneSenderBindingHistory).where(
            CloneSenderBindingHistory.task_id == task.id,
            CloneSenderBindingHistory.task_lifecycle_epoch == task.task_lifecycle_epoch,
            CloneSenderBindingHistory.status.in_(["active", "guarded", "eligible"]),
        )
        active_bindings = session.execute(active_bindings_stmt).scalars().all()
        used_account_ids = {b.assigned_account_id for b in active_bindings}

        # 查找完全未占用的账号
        available_account_ids = [acc_id for acc_id in allowed_account_ids if acc_id not in used_account_ids]

        # 如果有回复依赖，排除父发言人使用的账号 (Gate 2)
        excluded_for_reply: set[int] = set()
        if reply_to_sender_peer_id:
            parent_binding = session.execute(
                select(CloneSenderBindingHistory).where(
                    CloneSenderBindingHistory.task_id == task.id,
                    CloneSenderBindingHistory.task_lifecycle_epoch == task.task_lifecycle_epoch,
                    CloneSenderBindingHistory.source_sender_peer_id == reply_to_sender_peer_id,
                    CloneSenderBindingHistory.status.in_(["active", "guarded", "eligible"]),
                )
            ).scalar_one_or_none()
            if parent_binding:
                excluded_for_reply.add(parent_binding.assigned_account_id)

        clean_available = [acc_id for acc_id in available_account_ids if acc_id not in excluded_for_reply]

        chosen_account_id: Optional[int] = None
        if clean_available:
            chosen_account_id = clean_available[0]
        else:
            # 尝试从 eligible/guarded 中进行 LRU 回收
            candidates = [
                b for b in active_bindings
                if not b.is_vip and b.assigned_account_id not in excluded_for_reply
            ]
            # Gate 3: 最短持有期与冷却
            recyclable = [
                b for b in candidates
                if b.status == "eligible" or (b.last_spoken_at and (current_time - b.last_spoken_at).total_seconds() >= cooldown_sec)
            ]
            if recyclable:
                # 按最后发言时间升序（最久未发言优先回收）
                recyclable.sort(key=lambda x: x.last_spoken_at or datetime.min.replace(tzinfo=timezone.utc))
                reclaim_target = recyclable[0]
                reclaim_target.status = "expired"
                reclaim_target.valid_to = current_time
                reclaim_target.reassignment_reason = f"LRU 回收分配给 {source_sender_peer_id}"
                session.flush()
                chosen_account_id = reclaim_target.assigned_account_id

        if not chosen_account_id:
            return None, "sender_pool_exhausted: 号池全部占满且无满足冷却回收条件的账号"

        # 创建新 binding
        new_binding = CloneSenderBindingHistory(
            task_id=task.id,
            task_lifecycle_epoch=task.task_lifecycle_epoch,
            binding_version=1,
            source_sender_peer_type=source_sender_peer_type,
            source_sender_peer_id=source_sender_peer_id,
            source_sender_name=source_sender_name,
            assigned_account_id=chosen_account_id,
            status="active",
            is_vip=is_vip,
            valid_from=current_time,
            last_spoken_at=current_time,
            last_reassigned_at=current_time,
        )
        session.add(new_binding)
        session.flush()
        return new_binding, ""


# ---------------------------------------------------------------------------
# 3. 相册聚合器 (§8.2)
# ---------------------------------------------------------------------------
class CloneAlbumAggregator:
    @staticmethod
    def process_album_item(
        session: Session,
        task: Task,
        source_event: CloneSourceEvent,
        quiet_seconds: float = 1.5,
        max_seconds: float = 10.0,
    ) -> Tuple[CloneAlbumManifest, bool]:
        """
        聚合相册分片，判断是否静默窗口就绪。
        """
        grouped_id = source_event.grouped_id
        if not grouped_id:
            raise ValueError("source_event does not have grouped_id")

        current_time = datetime.now(timezone.utc)
        stmt = (
            select(CloneAlbumManifest)
            .where(
                CloneAlbumManifest.task_id == task.id,
                CloneAlbumManifest.epoch == task.task_lifecycle_epoch,
                CloneAlbumManifest.grouped_id == grouped_id,
            )
            .with_for_update()
        )
        manifest = session.execute(stmt).scalar_one_or_none()

        if not manifest:
            manifest = CloneAlbumManifest(
                task_id=task.id,
                epoch=task.task_lifecycle_epoch,
                grouped_id=grouped_id,
                first_observed_at=current_time,
                last_observed_at=current_time,
                quiet_deadline_at=current_time + timedelta(seconds=quiet_seconds),
                max_deadline_at=current_time + timedelta(seconds=max_seconds),
                items_total=1,
                state="collecting",
            )
            session.add(manifest)
            session.flush()
        else:
            manifest.last_observed_at = current_time
            manifest.quiet_deadline_at = current_time + timedelta(seconds=quiet_seconds)
            manifest.items_total += 1
            manifest.version += 1
            session.flush()

        # 记录分片 item
        item_stmt = select(CloneAlbumItem).where(
            CloneAlbumItem.manifest_id == manifest.id,
            CloneAlbumItem.source_message_id == source_event.source_message_id,
        )
        item = session.execute(item_stmt).scalar_one_or_none()
        if not item:
            item = CloneAlbumItem(
                manifest_id=manifest.id,
                source_event_id=source_event.id,
                part_index=manifest.items_total - 1,
                source_message_id=source_event.source_message_id,
                media_type=source_event.media_type or "photo",
                media_snapshot={},
                item_fingerprint=source_event.content_fingerprint,
                acquisition_state="acquired",
            )
            session.add(item)
            session.flush()

        return manifest, (manifest.state == "ready")


# ---------------------------------------------------------------------------
# 4. Sequencer 调度器与时序保序 (§9.1, §9.2)
# ---------------------------------------------------------------------------
class CloneSequencer:
    @staticmethod
    def calculate_human_planned_at(
        session: Session,
        task: Task,
        stream_order_no: int,
        delay_min_seconds: float = 3.0,
        delay_max_seconds: float = 8.0,
    ) -> datetime:
        """
        基于前序最大 planned_at 单调递增拟人随机延迟。
        """
        current_time = datetime.now(timezone.utc)
        stmt = (
            select(func.max(CloneDeliveryObligation.planned_at))
            .where(
                CloneDeliveryObligation.task_id == task.id,
                CloneDeliveryObligation.epoch == task.task_lifecycle_epoch,
                CloneDeliveryObligation.stream_order_no < stream_order_no,
            )
        )
        prev_max = session.execute(stmt).scalar_one_or_none()

        base_time = max(current_time, prev_max) if prev_max else current_time
        jitter = random.uniform(delay_min_seconds, delay_max_seconds)
        return base_time + timedelta(seconds=jitter)


# ---------------------------------------------------------------------------
# 5. Task Center Executor 主入口 (§18.1)
# ---------------------------------------------------------------------------
class GroupCloneExecutor:
    def build_plan(self, session: Session, task: Task) -> int:
        """
        规划周期：
        1. 验证目标群写入权威 (TelegramGroupMutationAuthority)
        2. 消费 Update Deliveries 并生成 CloneSourceEvents
        3. 物化未处理的 CloneDeliveryObligation 与 Action
        """
        if task.status != "running":
            return 0

        type_config = task.type_config or {}
        source_config = type_config.get("source", {})
        target_config = type_config.get("target", {})
        pacing_config = type_config.get("pacing", {})

        target_peer_type = target_config.get("peer_type", "channel")
        target_peer_id = str(target_config.get("peer_id", target_config.get("group_id", "")))
        source_peer_type = source_config.get("peer_type", "channel")
        source_peer_id = str(source_config.get("peer_id", source_config.get("group_id", "")))

        if not target_peer_id or not source_peer_id:
            task.status = "failed"
            task.last_error = "缺少源群或目标群配置"
            return 0

        route_hash = compute_route_hash(source_peer_type, source_peer_id, target_peer_type, target_peer_id)

        # 1. 独占写权限检查与领取
        claimed, err_msg, auth = check_and_claim_exclusive_authority(
            session=session,
            tenant_id=task.tenant_id,
            target_peer_type=target_peer_type,
            target_peer_id=target_peer_id,
            writer_kind="group_clone",
            writer_id=task.id,
            route_hash=route_hash,
        )
        if not claimed:
            task.status = "paused"
            task.last_error = f"Target Mutation Authority 冲突: {err_msg}"
            return 0

        # 2. 查找任务未物化的 Source Events
        events_stmt = (
            select(CloneSourceEvent)
            .where(
                CloneSourceEvent.task_id == task.id,
                CloneSourceEvent.task_lifecycle_epoch == task.task_lifecycle_epoch,
            )
            .order_by(CloneSourceEvent.stream_order_no.asc())
        )
        events = session.execute(events_stmt).scalars().all()

        materialized_count = 0
        for ev in events:
            # 检查是否已物化 obligation
            obl_stmt = select(CloneDeliveryObligation).where(
                CloneDeliveryObligation.task_id == task.id,
                CloneDeliveryObligation.epoch == task.task_lifecycle_epoch,
                CloneDeliveryObligation.source_event_id == ev.id,
            )
            existing_obl = session.execute(obl_stmt).scalar_one_or_none()
            if existing_obl:
                continue

            # 分配发言人
            binding = None
            if ev.sender_peer_id:
                binding, bind_err = CloneSenderBindingManager.get_or_assign_sender_binding(
                    session=session,
                    task=task,
                    source_sender_peer_type=ev.sender_peer_type or "user",
                    source_sender_peer_id=ev.sender_peer_id,
                    source_sender_name="",
                    reply_to_sender_peer_id=None,
                )
                if not binding:
                    # 无法分配发言人，暂缓物化
                    logger.warning("GroupClone %s: 无法分配发言人: %s", task.id, bind_err)
                    continue

            # 计算拟人延迟计划时间
            planned_at = CloneSequencer.calculate_human_planned_at(
                session=session,
                task=task,
                stream_order_no=ev.stream_order_no,
                delay_min_seconds=float(pacing_config.get("delay_min_seconds", 3.0)),
                delay_max_seconds=float(pacing_config.get("delay_max_seconds", 8.0)),
            )

            # 创建 Obligation
            obligation = CloneDeliveryObligation(
                tenant_id=task.tenant_id,
                task_id=task.id,
                epoch=task.task_lifecycle_epoch,
                source_event_id=ev.id,
                obligation_kind="send" if ev.event_type == "message_new" else ev.event_type.replace("message_", ""),
                materialization_version=1,
                stream_order_no=ev.stream_order_no,
                sequencer_id=ev.stream_order_no,
                binding_history_id=binding.id if binding else None,
                planned_at=planned_at,
                state="ready",
            )
            session.add(obligation)
            session.flush()

            # 创建 FOP 履约投影
            fop = FulfillmentObligationProjection(
                tenant_id=task.tenant_id,
                task_id=task.id,
                task_lifecycle_epoch=task.task_lifecycle_epoch,
                obligation_type="group_clone_delivery",
                obligation_id=obligation.id,
                work_lane="interaction",
                opened_at=now(),
                state="open",
                version=1,
            )
            session.add(fop)
            session.flush()

            # 生成不可变 random_id 并持久化 Mutation Identity (§10.1)
            random_id = derive_deterministic_random_id(
                contract="v2_group_clone",
                tenant_id=task.tenant_id,
                task_id=task.id,
                epoch=task.task_lifecycle_epoch,
                obligation_id=obligation.id,
                mutation_kind="sendMessage",
                part_index=0,
            )
            mutation_identity = TelegramGatewayMutationIdentity(
                tenant_id=task.tenant_id,
                task_id=task.id,
                epoch=task.task_lifecycle_epoch,
                obligation_id=obligation.id,
                materialization_version=1,
                mutation_kind="sendMessage",
                part_index=0,
                execution_role="sender",
                account_id=binding.assigned_account_id if binding else 0,
                telegram_account_peer_id=str(binding.assigned_account_id if binding else 0),
                authorization_id=0,
                target_peer_type=target_peer_type,
                target_peer_id=target_peer_id,
                random_id=random_id,
                request_fingerprint=ev.content_fingerprint,
                state="allocated",
            )
            session.add(mutation_identity)
            session.flush()

            # 创建 Action
            action = Action(
                tenant_id=task.tenant_id,
                task_id=task.id,
                action_type="group_clone_send",
                status="pending",
                account_id=binding.assigned_account_id if binding else None,
                target_peer_type=target_peer_type,
                target_peer_id=target_peer_id,
                payload={
                    "obligation_id": obligation.id,
                    "content": ev.content,
                    "random_id": random_id,
                    "stream_order_no": ev.stream_order_no,
                    "source_message_id": ev.source_message_id,
                },
                scheduled_at=planned_at,
            )
            session.add(action)
            session.flush()

            fop.active_action_id = action.id
            materialized_count += 1

        return materialized_count


group_clone = GroupCloneExecutor()

__all__ = [
    "CloneAlbumAggregator",
    "CloneSenderBindingManager",
    "CloneSequencer",
    "GroupCloneExecutor",
    "derive_deterministic_random_id",
    "group_clone",
]
