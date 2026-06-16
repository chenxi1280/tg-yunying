from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import AccountStatus, Action, GroupAuthStatus, OperationTarget, Task, TgAccount, TgGroup, TgGroupAccount, VerificationTask
from app.services._common import _now

from .account_pool import select_task_accounts
from .membership_recovery import AUTO_RETRY_BUCKET, VERIFICATION_BUCKET, classify_membership_recovery
from .pacing import schedule_times
from .payloads import EnsureChannelMembershipPayload, create_membership_action
from .targets import group_from_reference


ACTION_TYPE = "ensure_target_membership"
LEGACY_ACTION_TYPE = "ensure_channel_membership"
OPEN_STATUSES = {"pending", "claiming", "executing", "retryable_failed"}
AI_GROUP_MEMBERSHIP_SCHEDULE_WINDOW_HOURS = 4
AI_GROUP_MEMBERSHIP_SCHEDULE_WINDOW_SECONDS = AI_GROUP_MEMBERSHIP_SCHEDULE_WINDOW_HOURS * 3600
HARD_HOURLY_MEMBERSHIP_FAST_TRACK_INTERVAL_SECONDS = 2
HARD_HOURLY_AUTO_VERIFICATION_RETRY_SECONDS = 300
HARD_HOURLY_MEMBERSHIP_RETRY_SECONDS = 300
AUTO_VERIFICATION_RETRY_STATUSES = {"待处理", "失败", "需人工处理"}
REQUIRED_CHANNEL_RETRY_MARKERS = ("需要关注", "关注我们的频道", "t.me/", "telegram.me/", "required channel")


@dataclass(frozen=True)
class MembershipGateResult:
    ready: bool
    created: int = 0
    waiting: bool = False
    blocked: bool = False
    blocker_reason: str = ""


def gate_channel_membership(session: Session, task: Task, channel: OperationTarget, *, require_send: bool = False) -> MembershipGateResult:
    candidates = candidate_accounts_for_config(session, task.tenant_id, task.account_config or {})
    strategy_enabled, disabled_reason = _membership_action_strategy(task, channel)
    reactivated = _reactivate_auto_verification_memberships(
        session,
        task,
        channel,
        candidates,
        require_send=require_send,
    ) if strategy_enabled else 0
    summary = channel_membership_summary(session, task.tenant_id, channel, task.account_config or {}, candidates=candidates, task_id=task.id, require_send=require_send)
    stats = _merge_membership_stats(task, summary)
    if reactivated:
        stats["membership_reactivated_verification_actions"] = int(stats.get("membership_reactivated_verification_actions") or 0) + reactivated
    if not _target_requires_membership_for_candidates(channel, candidates, require_send=require_send):
        stats["membership_stage"] = "membership_ready"
        task.stats = stats
        if task.last_error in {"正在执行关注频道前置阶段", "正在执行目标准入前置阶段", "没有账号成功准备目标"}:
            task.last_error = ""
        return MembershipGateResult(True)
    if not candidates:
        task.last_error = f"没有匹配账号，无法准备目标{_target_noun(channel)}"
        stats["membership_stage"] = "membership_blocked"
        task.stats = stats
        return MembershipGateResult(False, blocked=True, blocker_reason="account_unavailable")

    ready_count = int(summary.get("joined_account_count") or 0)
    open_count = _open_membership_action_count(session, task)
    if not strategy_enabled:
        stats["membership_stage"] = "membership_partial" if ready_count > 0 else "membership_blocked"
        task.stats = stats
        if ready_count > 0:
            if task.last_error == disabled_reason:
                task.last_error = ""
            return MembershipGateResult(True)
        task.last_error = disabled_reason
        return MembershipGateResult(False, blocked=True, blocker_reason="target_membership_disabled")
    created = _create_missing_membership_actions(session, task, channel, candidates, require_send=require_send)
    fast_tracked = _fast_track_hard_hourly_membership_actions(session, task, channel)
    if created:
        stats["membership_stage"] = "membership_running"
        stats["membership_created_actions"] = int(stats.get("membership_created_actions") or 0) + created
        if _uses_four_hour_membership_window(task, channel, require_send=require_send):
            stats["membership_schedule_window_hours"] = AI_GROUP_MEMBERSHIP_SCHEDULE_WINDOW_HOURS
        _record_fast_tracked_memberships(stats, fast_tracked)
        task.stats = stats
        if ready_count > 0:
            if task.last_error in {"正在执行关注频道前置阶段", "没有账号成功关注目标频道", "正在执行目标准入前置阶段", "没有账号成功准备目标"}:
                task.last_error = ""
            return MembershipGateResult(True, created=created, waiting=True)
        task.last_error = "正在执行目标准入前置阶段"
        return MembershipGateResult(False, created=created, waiting=True)
    if open_count:
        stats["membership_stage"] = "membership_running"
        _record_fast_tracked_memberships(stats, fast_tracked)
        task.stats = stats
        if ready_count > 0:
            if task.last_error in {"正在执行频道关注前置阶段", "正在执行关注频道前置阶段", "正在执行目标准入前置阶段"}:
                task.last_error = ""
            return MembershipGateResult(True, created=reactivated, waiting=True)
        task.last_error = "正在执行目标准入前置阶段"
        return MembershipGateResult(False, created=reactivated, waiting=True)

    refreshed = channel_membership_summary(session, task.tenant_id, channel, task.account_config or {}, candidates=candidates, task_id=task.id, require_send=require_send)
    stats = _merge_membership_stats(task, refreshed)
    if refreshed["joined_account_count"] <= 0:
        stats["membership_stage"] = "membership_blocked"
        task.stats = stats
        task.last_error = "没有账号成功准备目标"
        return MembershipGateResult(False, blocked=True, blocker_reason=_membership_blocker_reason(refreshed))

    stats["membership_stage"] = "membership_ready" if refreshed["failed_account_count"] == 0 else "membership_partial"
    task.stats = stats
    if task.last_error in {"正在执行关注频道前置阶段", "没有账号成功关注目标频道", "正在执行目标准入前置阶段", "没有账号成功准备目标"}:
        task.last_error = ""
    return MembershipGateResult(True)


def channel_member_accounts(session: Session, task: Task, channel: OperationTarget, accounts: list[TgAccount], *, require_send: bool = False) -> list[TgAccount]:
    if not require_send and not _target_requires_membership_for_candidates(channel, accounts):
        return accounts
    group = linked_channel_group(session, channel, create=False, prefer_send_ready=require_send)
    directly_ready_ids = _directly_ready_channel_account_ids(channel, accounts, require_send=require_send)
    if not group:
        return [account for account in accounts if account.id in directly_ready_ids]
    member_ids, blocked_send_ids = _channel_member_id_sets(session, task.tenant_id, group.id, require_send=require_send)
    return [account for account in accounts if account.id not in blocked_send_ids and (account.id in member_ids or account.id in directly_ready_ids)]


def _channel_member_id_sets(session: Session, tenant_id: int, group_id: int, *, require_send: bool) -> tuple[set[int], set[int]]:
    links = session.scalars(
        select(TgGroupAccount).where(
            TgGroupAccount.tenant_id == tenant_id,
            TgGroupAccount.group_id == group_id,
        )
    )
    member_ids: set[int] = set()
    blocked_send_ids: set[int] = set()
    for link in links:
        if require_send and link.can_send is False:
            blocked_send_ids.add(int(link.account_id))
            continue
        member_ids.add(int(link.account_id))
    return member_ids, blocked_send_ids


def _directly_ready_channel_account_ids(channel: OperationTarget, accounts: list[TgAccount], *, require_send: bool) -> set[int]:
    return {account.id for account in accounts if account_satisfies_authorized_target(channel, account, require_send=require_send)}


def mark_channel_membership_joined(session: Session, tenant_id: int, channel_target_id: int, account_id: int, *, permission_label: str = "已关注") -> None:
    channel = session.get(OperationTarget, channel_target_id)
    if not channel or channel.tenant_id != tenant_id:
        raise ValueError("operation target not found")
    group = linked_channel_group(session, channel, create=True)
    link = session.scalar(
        select(TgGroupAccount).where(
            TgGroupAccount.tenant_id == tenant_id,
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.account_id == account_id,
        )
    )
    if link is None:
        link = TgGroupAccount(tenant_id=tenant_id, group_id=group.id, account_id=account_id)
        session.add(link)
    target_can_send = bool(channel.can_send) if channel.target_type == "channel" else True
    link.permission_label = permission_label
    link.can_send = bool(link.can_send or target_can_send)
    group.auth_status = GroupAuthStatus.AUTHORIZED.value
    group.can_send = bool(group.can_send or target_can_send)
    channel.auth_status = GroupAuthStatus.AUTHORIZED.value
    channel.can_send = bool(channel.can_send or target_can_send)
    channel.updated_at = _now()


def channel_membership_summary(
    session: Session,
    tenant_id: int,
    channel: OperationTarget,
    account_config: dict[str, Any],
    *,
    candidates: list[TgAccount] | None = None,
    task_id: str | None = None,
    require_send: bool = False,
) -> dict[str, Any]:
    candidate_rows = candidates if candidates is not None else candidate_accounts_for_config(session, tenant_id, account_config)
    candidate_ids = [account.id for account in candidate_rows]
    group = linked_channel_group(session, channel, create=False, prefer_send_ready=require_send)
    joined_ids: set[int] = {account.id for account in candidate_rows if account_satisfies_authorized_target(channel, account, require_send=require_send)}
    if group and candidate_ids:
        link_stmt = select(TgGroupAccount.account_id).where(
            TgGroupAccount.tenant_id == tenant_id,
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.account_id.in_(candidate_ids),
        )
        if require_send and channel.target_type == "group":
            link_stmt = link_stmt.where(TgGroupAccount.can_send.is_(True))
        joined_ids.update(int(account_id) for account_id in session.scalars(link_stmt))
    terminal_actions = _membership_actions_by_account(session, channel.id, task_id=task_id)
    failed_ids = {account_id for account_id, action in terminal_actions.items() if account_id in candidate_ids and _is_failed_membership_action(action)}
    return {
        "channel_target_id": channel.id,
        "channel_title": channel.title,
        "target_type": channel.target_type,
        "subtask_type": "target_membership",
        "require_send": require_send,
        "candidate_account_count": len(candidate_ids),
        "joined_account_count": len(joined_ids),
        "need_join_account_count": len([account_id for account_id in candidate_ids if account_id not in joined_ids and account_id not in failed_ids]),
        "failed_account_count": len(failed_ids),
        "blocked_account_count": len([account for account in candidate_rows if account.status != AccountStatus.ACTIVE.value]),
        "joined_account_ids": sorted(joined_ids),
        "failed_account_ids": sorted(failed_ids),
        "estimated_membership_actions": len([account_id for account_id in candidate_ids if account_id not in joined_ids]),
    }


def candidate_accounts_for_config(session: Session, tenant_id: int, account_config: dict[str, Any]) -> list[TgAccount]:
    stmt = (
        select(TgAccount)
        .where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.status == AccountStatus.ACTIVE.value,
        )
        .order_by(TgAccount.health_score.desc(), TgAccount.id.asc())
    )
    mode = account_config.get("selection_mode") or "all"
    if mode == "manual":
        account_ids = [int(item) for item in account_config.get("account_ids") or []]
        if not account_ids:
            stmt = None
        else:
            account_order = case(
                {account_id: index for index, account_id in enumerate(account_ids)},
                value=TgAccount.id,
            )
            stmt = stmt.where(TgAccount.id.in_(account_ids)).order_by(None).order_by(account_order.asc())
    elif mode == "group":
        pool_id = int(account_config.get("account_group_id") or 0)
        if not pool_id:
            stmt = None
        else:
            stmt = stmt.where(TgAccount.pool_id == pool_id)
    return list(session.scalars(stmt)) if stmt is not None else []


def linked_channel_group(session: Session, channel: OperationTarget, *, create: bool, prefer_send_ready: bool = False) -> TgGroup | None:
    if prefer_send_ready and channel.target_type == "group":
        group = group_from_reference(session, channel.tenant_id, operation_target_id=channel.id, require_authorized=False)
        if group:
            return group
    group = session.scalar(
        select(TgGroup).where(
            TgGroup.tenant_id == channel.tenant_id,
            TgGroup.tg_peer_id == channel.tg_peer_id,
        )
    )
    if group or not create:
        return group
    group = TgGroup(
        tenant_id=channel.tenant_id,
        tg_peer_id=channel.tg_peer_id,
        title=channel.title,
        group_type="channel" if channel.target_type == "channel" else "supergroup",
        member_count=channel.member_count,
        auth_status=channel.auth_status,
        can_send=channel.can_send,
    )
    session.add(group)
    session.flush()
    return group


def channel_requires_membership_gate(channel: OperationTarget) -> bool:
    return target_requires_membership_gate(channel)


def target_requires_membership_gate(target: OperationTarget, *, require_send: bool = False) -> bool:
    if target.auth_status not in _AUTHORIZED_TARGET_VALUES:
        return True
    if require_send and target.target_type == "group" and not bool(target.can_send):
        return True
    if target.target_type == "channel":
        return not bool(target.can_send)
    return False


_AUTHORIZED_TARGET_VALUES = {GroupAuthStatus.AUTHORIZED.value, "已授权", "授权", "正常"}


def account_satisfies_authorized_target(target: OperationTarget, account: TgAccount, *, require_send: bool = False) -> bool:
    if target.auth_status not in _AUTHORIZED_TARGET_VALUES:
        return False
    if require_send and not target.can_send:
        return False
    if target.target_type != "channel":
        return False
    peer_id = str(target.tg_peer_id or "")
    return bool(target.can_send and (account.session_ciphertext or peer_id.startswith("-100")))


def _target_requires_membership_for_candidates(target: OperationTarget, candidates: list[TgAccount], *, require_send: bool = False) -> bool:
    if target_requires_membership_gate(target, require_send=require_send):
        return True
    if not candidates:
        return False
    return any(not account_satisfies_authorized_target(target, account, require_send=require_send) for account in candidates)


def _create_missing_membership_actions(session: Session, task: Task, channel: OperationTarget, candidates: list[TgAccount], *, require_send: bool = False) -> int:
    existing = _membership_actions_by_account(session, channel.id, task_id=task.id)
    group = linked_channel_group(session, channel, create=True, prefer_send_ready=require_send)
    joined_ids = _ready_membership_account_ids(session, task, channel, group, candidates, require_send=require_send)
    now_value = _now()
    missing = _membership_retry_candidates(candidates, existing, joined_ids, task, now_value)
    random.shuffle(missing)
    if not missing:
        return 0
    return _create_membership_actions_for_accounts(session, task, channel, joined_ids, missing, now_value, require_send=require_send)


def _ready_membership_account_ids(
    session: Session,
    task: Task,
    channel: OperationTarget,
    group: TgGroup,
    candidates: list[TgAccount],
    *,
    require_send: bool,
) -> set[int]:
    link_stmt = select(TgGroupAccount.account_id).where(TgGroupAccount.tenant_id == task.tenant_id, TgGroupAccount.group_id == group.id)
    if require_send:
        link_stmt = link_stmt.where(TgGroupAccount.can_send.is_(True))
    joined_ids = {int(account_id) for account_id in session.scalars(link_stmt)}
    joined_ids.update(_directly_ready_channel_account_ids(channel, candidates, require_send=require_send))
    return joined_ids


def _membership_retry_candidates(
    candidates: list[TgAccount],
    existing: dict[int, Action],
    joined_ids: set[int],
    task: Task,
    now_value,
) -> list[TgAccount]:
    return [
        account
        for account in candidates
        if _should_create_membership_attempt_for_account(account.id, existing.get(account.id), joined_ids, task, now_value)
    ]


def _should_create_membership_attempt_for_account(
    account_id: int,
    action: Action | None,
    joined_ids: set[int],
    task: Task,
    now_value,
) -> bool:
    if account_id in joined_ids:
        return action is None
    return _should_create_membership_attempt(action, task, now_value)


def _create_membership_actions_for_accounts(
    session: Session,
    task: Task,
    channel: OperationTarget,
    joined_ids: set[int],
    missing: list[TgAccount],
    now_value,
    *,
    require_send: bool,
) -> int:
    pending_count = len([account for account in missing if account.id not in joined_ids])
    scheduled_times = _membership_schedule_times(task, channel, pending_count, now_value, require_send=require_send)
    scheduled_index = 0
    created = 0
    with session.no_autoflush:
        for account in missing:
            payload = EnsureChannelMembershipPayload(
                channel_id=channel.tg_peer_id,
                channel_target_id=channel.id,
                target_type=channel.target_type,
                target_display=channel.title,
                target_username=channel.username or "",
                invite_link=_joinable_channel_reference(channel),
                require_send=require_send,
            )
            if account.id in joined_ids:
                action = create_membership_action(session, task, account.id, now_value, payload, flush=False)
                action.status = "skipped"
                action.executed_at = now_value
                action.result = {"success": True, "membership_status": "already_joined", "detail": f"账号已满足目标{_target_noun(channel)}准入"}
                created += 1
                continue
            scheduled_at = scheduled_times[scheduled_index] if scheduled_index < len(scheduled_times) else now_value
            scheduled_index += 1
            action = create_membership_action(session, task, account.id, scheduled_at, payload, flush=False)
            if task.type == "group_ai_chat" and (task.type_config or {}).get("hard_hourly_target_enabled"):
                action.result = {**(action.result or {}), "retry_reason": "hard_hourly_membership_retry"}
            created += 1
    if created:
        session.flush()
    return created


def _should_create_membership_attempt(action: Action | None, task: Task, now_value) -> bool:
    if action is None:
        return True
    if action.status in OPEN_STATUSES:
        return False
    if _is_failed_membership_action(action):
        return False
    if not _hard_hourly_membership_fast_track_enabled(task):
        return False
    last_attempt_at = action.executed_at or action.scheduled_at or action.created_at
    if not last_attempt_at:
        return True
    return (now_value - last_attempt_at.replace(tzinfo=None)).total_seconds() >= HARD_HOURLY_MEMBERSHIP_RETRY_SECONDS


def _reactivate_auto_verification_memberships(
    session: Session,
    task: Task,
    channel: OperationTarget,
    candidates: list[TgAccount],
    *,
    require_send: bool,
) -> int:
    if not require_send or task.type != "group_ai_chat":
        return 0
    group = linked_channel_group(session, channel, create=False, prefer_send_ready=require_send)
    if not group:
        return 0
    latest_actions = _membership_actions_by_account(session, channel.id, task_id=task.id)
    candidate_ids = {int(account.id) for account in candidates}
    failed_actions = [
        action
        for account_id, action in latest_actions.items()
        if account_id in candidate_ids and action.account_id and _is_failed_membership_action(action)
    ]
    verification_by_account = _auto_verification_tasks_by_account(session, task, group.id, [int(action.account_id) for action in failed_actions])
    account_by_id = {int(account.id): account for account in candidates}
    now_value = _now()
    created = 0
    rows: list[dict[str, Any]] = []
    for action in failed_actions:
        account_id = int(action.account_id or 0)
        verification = verification_by_account.get(account_id)
        account = account_by_id.get(account_id)
        reason = _membership_recovery_retry_reason(task, action, account, verification, now_value)
        if not reason:
            continue
        rows.append(_membership_retry_action_row(task, action, now_value, created, reason, verification.id if verification else None))
        created += 1
    if rows:
        session.bulk_insert_mappings(Action, rows)
    return created


def _membership_recovery_retry_reason(
    task: Task,
    action: Action,
    account: TgAccount | None,
    verification: VerificationTask | None,
    now_value,
) -> str:
    result = action.result if isinstance(action.result, dict) else {}
    failure_type = str(result.get("error_code") or "")
    failure_detail = str(result.get("error_message") or result.get("detail") or result.get("failure_detail") or "")
    recovery = classify_membership_recovery(
        phase="failed",
        account_status=account.status if account else "",
        action_status=action.status,
        failure_type=failure_type,
        failure_detail=failure_detail,
        verification_action=verification.suggested_action if verification else "",
        verification_status=verification.status if verification else "",
        can_auto_resolve=bool(verification.can_auto_resolve) if verification else False,
    )
    if recovery.bucket == VERIFICATION_BUCKET and verification and _auto_verification_retry_due(action, verification, now_value):
        return _reactivation_reason(task, "auto_verification")
    if recovery.bucket != AUTO_RETRY_BUCKET:
        return ""
    if verification and _auto_verification_retry_due(action, verification, now_value):
        return _reactivation_reason(task, "auto_verification")
    if _required_channel_retry_due(action, now_value):
        return _reactivation_reason(task, "required_channel")
    return ""


def _reactivation_reason(task: Task, reason_type: str) -> str:
    if _hard_hourly_membership_fast_track_enabled(task):
        if reason_type == "auto_verification":
            return "hard_hourly_auto_verification_retry"
        return "hard_hourly_required_channel_retry"
    if reason_type == "auto_verification":
        return "membership_recovery_auto_verification"
    return "membership_recovery_required_channel"


def _membership_retry_action_row(
    task: Task,
    action: Action,
    now_value,
    offset: int,
    reason: str,
    verification_id: int | None = None,
) -> dict[str, Any]:
    payload = EnsureChannelMembershipPayload.model_validate(action.payload or {})
    scheduled_at = now_value + timedelta(seconds=HARD_HOURLY_MEMBERSHIP_FAST_TRACK_INTERVAL_SECONDS * offset)
    result = {"reactivated_reason": reason}
    if verification_id:
        result["verification_task_id"] = verification_id
    return {
        "id": str(uuid4()),
        "tenant_id": task.tenant_id,
        "task_id": task.id,
        "task_type": task.type,
        "action_type": ACTION_TYPE,
        "account_id": int(action.account_id),
        "scheduled_at": scheduled_at,
        "status": "pending",
        "lease_owner": "",
        "claim_owner": "",
        "claim_token": "",
        "plan_batch_key": f"{task.id}:hard-hourly-auto-verification:{now_value.isoformat()}",
        "action_dedupe_key": (
            f"{task.tenant_id}:{task.id}:hard-hourly-auto-verification:"
            f"{action.account_id}:{reason}:{verification_id or 'required-channel'}:{scheduled_at.isoformat()}"
        ),
        "payload": payload.model_dump(mode="json"),
        "result": result,
        "retry_count": 0,
        "created_at": now_value,
    }


def _auto_verification_tasks_by_account(
    session: Session,
    task: Task,
    group_id: int,
    account_ids: list[int],
) -> dict[int, VerificationTask]:
    if not account_ids:
        return {}
    rows = session.scalars(
        select(VerificationTask)
        .where(
            VerificationTask.tenant_id == task.tenant_id,
            VerificationTask.group_id == group_id,
            VerificationTask.account_id.in_(account_ids),
            VerificationTask.status.in_(AUTO_VERIFICATION_RETRY_STATUSES),
        )
        .order_by(VerificationTask.id.desc())
    )
    latest: dict[int, VerificationTask] = {}
    for verification in rows:
        if verification.account_id and verification.account_id not in latest and verification.can_auto_resolve:
            latest[int(verification.account_id)] = verification
    return latest


def _auto_verification_retry_due(action: Action, verification: VerificationTask, now_value) -> bool:
    if verification.status not in AUTO_VERIFICATION_RETRY_STATUSES or not verification.can_auto_resolve:
        return False
    last_attempt_at = verification.handled_at or action.executed_at or action.created_at
    if not last_attempt_at:
        return True
    return (now_value - last_attempt_at.replace(tzinfo=None)).total_seconds() >= HARD_HOURLY_AUTO_VERIFICATION_RETRY_SECONDS


def _required_channel_retry_due(action: Action, now_value) -> bool:
    if not _membership_failure_mentions_required_channel(action):
        return False
    last_attempt_at = action.executed_at or action.scheduled_at or action.created_at
    if not last_attempt_at:
        return True
    return (now_value - last_attempt_at.replace(tzinfo=None)).total_seconds() >= HARD_HOURLY_MEMBERSHIP_RETRY_SECONDS


def _membership_failure_mentions_required_channel(action: Action) -> bool:
    result = action.result if isinstance(action.result, dict) else {}
    detail = " ".join(
        str(value or "")
        for value in (
            result.get("error_message"),
            result.get("detail"),
            result.get("failure_detail"),
            result.get("detected_reason"),
        )
    ).lower()
    return any(marker.lower() in detail for marker in REQUIRED_CHANNEL_RETRY_MARKERS)


def _membership_action_strategy(task: Task, channel: OperationTarget) -> tuple[bool, str]:
    config = task.type_config or {}
    if channel.target_type == "channel":
        return bool(config.get("auto_follow_required_channel", True)), "准入策略已关闭自动关注关联频道"
    return bool(config.get("auto_join_target", True)), "准入策略已关闭自动入群"


def _membership_pacing_config(task: Task) -> dict[str, Any]:
    config = dict(task.pacing_config or {})
    membership_concurrent = int((task.type_config or {}).get("membership_max_concurrent") or 0)
    if membership_concurrent:
        config["max_concurrent"] = membership_concurrent
    if (config.get("mode") or "template") == "fixed":
        return config
    return {**config, "template": config.get("template") or "moderate_6h"}


def _membership_schedule_times(
    task: Task,
    channel: OperationTarget,
    pending_count: int,
    now_value,
    *,
    require_send: bool,
) -> list:
    if _uses_four_hour_membership_window(task, channel, require_send=require_send):
        return _four_hour_membership_schedule(pending_count, now_value)
    return schedule_times(pending_count, _membership_pacing_config(task), start_at=now_value)


def _uses_four_hour_membership_window(task: Task, channel: OperationTarget, *, require_send: bool) -> bool:
    return task.type == "group_ai_chat" and channel.target_type == "group" and require_send


def _four_hour_membership_schedule(pending_count: int, now_value) -> list:
    if pending_count <= 0:
        return []
    if pending_count == 1:
        return [now_value]
    step_seconds = AI_GROUP_MEMBERSHIP_SCHEDULE_WINDOW_SECONDS / max(1, pending_count - 1)
    return [now_value + timedelta(seconds=int(step_seconds * index)) for index in range(pending_count)]


def _fast_track_hard_hourly_membership_actions(session: Session, task: Task, channel: OperationTarget) -> int:
    if not _hard_hourly_membership_fast_track_enabled(task):
        return 0
    now_value = _now()
    rows = list(
        session.scalars(
            select(Action)
            .where(
                Action.task_id == task.id,
                Action.action_type.in_([ACTION_TYPE, LEGACY_ACTION_TYPE]),
                Action.status == "pending",
                Action.scheduled_at > now_value,
                Action.payload["channel_target_id"].as_integer() == channel.id,
            )
            .order_by(Action.scheduled_at.asc(), Action.created_at.asc())
        )
    )
    for index, action in enumerate(rows):
        action.scheduled_at = now_value + timedelta(seconds=HARD_HOURLY_MEMBERSHIP_FAST_TRACK_INTERVAL_SECONDS * index)
        action.result = {**(action.result or {}), "fast_tracked_reason": "hard_hourly_membership"}
    return len(rows)


def _hard_hourly_membership_fast_track_enabled(task: Task) -> bool:
    config = task.type_config or {}
    return task.type == "group_ai_chat" and bool(config.get("hard_hourly_target_enabled")) and int(config.get("hourly_min_messages") or 0) > 0


def _record_fast_tracked_memberships(stats: dict[str, Any], count: int) -> None:
    if count:
        stats["membership_fast_tracked_actions"] = int(stats.get("membership_fast_tracked_actions") or 0) + count


def _joinable_channel_reference(channel: OperationTarget) -> str:
    if channel.username:
        return f"https://t.me/{channel.username.lstrip('@')}"
    if str(channel.tg_peer_id).startswith(("https://t.me/", "http://t.me/", "t.me/", "https://telegram.me/", "http://telegram.me/", "telegram.me/", "+")):
        return str(channel.tg_peer_id)
    return ""


def _membership_actions_by_account(session: Session, channel_target_id: int, *, task_id: str | None = None) -> dict[int, Action]:
    filters = [Action.action_type.in_([ACTION_TYPE, LEGACY_ACTION_TYPE]), Action.account_id.is_not(None), Action.payload["channel_target_id"].as_integer() == channel_target_id]
    if task_id:
        filters.append(Action.task_id == task_id)
    rows = list(session.scalars(select(Action).where(*filters).order_by(Action.created_at.desc())))
    result: dict[int, Action] = {}
    for action in rows:
        if action.account_id and action.account_id not in result:
            result[int(action.account_id)] = action
    return result


def _is_failed_membership_action(action: Action) -> bool:
    if action.status == "failed":
        return True
    if action.status != "skipped":
        return False
    result = action.result if isinstance(action.result, dict) else {}
    return result.get("error_code") == "membership_permission_denied" or result.get("membership_status") == "permission_denied"


def _membership_blocker_reason(summary: dict[str, Any]) -> str:
    candidate_count = int(summary.get("candidate_account_count") or 0)
    failed_count = int(summary.get("failed_account_count") or 0)
    joined_count = int(summary.get("joined_account_count") or 0)
    need_count = int(summary.get("need_join_account_count") or 0)
    if candidate_count > 0 and joined_count <= 0 and need_count <= 0 and failed_count >= candidate_count:
        return "target_permission"
    return "target_membership_pending"


def _open_membership_action_count(session: Session, task: Task) -> int:
    return int(
        session.scalar(
            select(func.count(Action.id)).where(
                Action.task_id == task.id,
                Action.action_type.in_([ACTION_TYPE, LEGACY_ACTION_TYPE]),
                Action.status.in_(OPEN_STATUSES),
            )
        )
        or 0
    )


def _merge_membership_stats(task: Task, summary: dict[str, Any]) -> dict[str, Any]:
    stats = dict(task.stats or {})
    stats["membership_summary"] = summary
    stats["membership_candidate_count"] = summary["candidate_account_count"]
    stats["membership_joined_count"] = summary["joined_account_count"]
    stats["membership_need_join_count"] = summary["need_join_account_count"]
    stats["membership_failed_count"] = summary["failed_account_count"]
    if not stats.get("membership_stage"):
        stats["membership_stage"] = "membership_pending"
    return stats


def _target_noun(target: OperationTarget) -> str:
    return "频道关注" if target.target_type == "channel" else "群聊加入"
