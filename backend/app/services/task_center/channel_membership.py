from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import AccountStatus, Action, GroupAuthStatus, OperationTarget, Task, TgAccount, TgGroup, TgGroupAccount
from app.services._common import _now

from .account_pool import select_task_accounts
from .pacing import schedule_times
from .payloads import EnsureChannelMembershipPayload, create_membership_action


ACTION_TYPE = "ensure_target_membership"
LEGACY_ACTION_TYPE = "ensure_channel_membership"
OPEN_STATUSES = {"pending", "claiming", "executing", "retryable_failed"}


@dataclass(frozen=True)
class MembershipGateResult:
    ready: bool
    created: int = 0
    waiting: bool = False
    blocked: bool = False
    blocker_reason: str = ""


def gate_channel_membership(session: Session, task: Task, channel: OperationTarget, *, require_send: bool = False) -> MembershipGateResult:
    candidates = candidate_accounts_for_config(session, task.tenant_id, task.account_config or {})
    summary = channel_membership_summary(session, task.tenant_id, channel, task.account_config or {}, candidates=candidates, task_id=task.id, require_send=require_send)
    stats = _merge_membership_stats(task, summary)
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
    strategy_enabled, disabled_reason = _membership_action_strategy(task, channel)
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
    if created:
        stats["membership_stage"] = "membership_running"
        stats["membership_created_actions"] = int(stats.get("membership_created_actions") or 0) + created
        task.stats = stats
        if ready_count > 0:
            if task.last_error in {"正在执行关注频道前置阶段", "没有账号成功关注目标频道", "正在执行目标准入前置阶段", "没有账号成功准备目标"}:
                task.last_error = ""
            return MembershipGateResult(True, created=created, waiting=True)
        task.last_error = "正在执行目标准入前置阶段"
        return MembershipGateResult(False, created=created, waiting=True)
    if open_count:
        stats["membership_stage"] = "membership_running"
        task.stats = stats
        if ready_count > 0:
            if task.last_error in {"正在执行频道关注前置阶段", "正在执行关注频道前置阶段", "正在执行目标准入前置阶段"}:
                task.last_error = ""
            return MembershipGateResult(True, waiting=True)
        task.last_error = "正在执行目标准入前置阶段"
        return MembershipGateResult(False, waiting=True)

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
    group = linked_channel_group(session, channel, create=False)
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
    group = linked_channel_group(session, channel, create=False)
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


def linked_channel_group(session: Session, channel: OperationTarget, *, create: bool) -> TgGroup | None:
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
    group = linked_channel_group(session, channel, create=True)
    link_stmt = select(TgGroupAccount.account_id).where(
        TgGroupAccount.tenant_id == task.tenant_id,
        TgGroupAccount.group_id == group.id,
    )
    if require_send:
        link_stmt = link_stmt.where(TgGroupAccount.can_send.is_(True))
    joined_ids = {int(account_id) for account_id in session.scalars(link_stmt)}
    joined_ids.update(_directly_ready_channel_account_ids(channel, candidates, require_send=require_send))
    missing = [account for account in candidates if account.id not in existing]
    random.shuffle(missing)
    if not missing:
        return 0
    scheduled_times = schedule_times(len([account for account in missing if account.id not in joined_ids]), _membership_pacing_config(task))
    scheduled_index = 0
    created = 0
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
            action = create_membership_action(session, task, account.id, _now(), payload)
            action.status = "skipped"
            action.executed_at = _now()
            action.result = {"success": True, "membership_status": "already_joined", "detail": f"账号已满足目标{_target_noun(channel)}准入"}
            created += 1
            continue
        scheduled_at = scheduled_times[scheduled_index] if scheduled_index < len(scheduled_times) else _now()
        scheduled_index += 1
        create_membership_action(session, task, account.id, scheduled_at, payload)
        created += 1
    return created


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
