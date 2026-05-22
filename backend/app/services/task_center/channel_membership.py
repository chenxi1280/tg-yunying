from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AccountPool, AccountStatus, Action, GroupAuthStatus, OperationTarget, Task, TgAccount, TgGroup, TgGroupAccount
from app.services._common import _now

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


def gate_channel_membership(session: Session, task: Task, channel: OperationTarget, *, require_send: bool = False) -> MembershipGateResult:
    candidates = candidate_accounts_for_config(session, task.tenant_id, task.account_config or {})
    summary = channel_membership_summary(session, task.tenant_id, channel, task.account_config or {}, candidates=candidates, task_id=task.id, require_send=require_send)
    stats = _merge_membership_stats(task, summary)
    if not candidates:
        task.last_error = f"没有匹配账号，无法准备目标{_target_noun(channel)}"
        stats["membership_stage"] = "membership_blocked"
        task.stats = stats
        return MembershipGateResult(False, blocked=True)

    open_count = _open_membership_action_count(session, task)
    created = _create_missing_membership_actions(session, task, channel, candidates, require_send=require_send)
    ready_count = int(summary.get("joined_account_count") or 0)
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
        return MembershipGateResult(False, blocked=True)

    stats["membership_stage"] = "membership_ready" if refreshed["failed_account_count"] == 0 else "membership_partial"
    task.stats = stats
    if task.last_error in {"正在执行关注频道前置阶段", "没有账号成功关注目标频道", "正在执行目标准入前置阶段", "没有账号成功准备目标"}:
        task.last_error = ""
    return MembershipGateResult(True)


def channel_member_accounts(session: Session, task: Task, channel: OperationTarget, accounts: list[TgAccount]) -> list[TgAccount]:
    group = linked_channel_group(session, channel, create=False)
    if not group:
        return []
    member_ids = {
        int(account_id)
        for account_id in session.scalars(
            select(TgGroupAccount.account_id).where(
                TgGroupAccount.tenant_id == task.tenant_id,
                TgGroupAccount.group_id == group.id,
            )
        )
    }
    return [account for account in accounts if account.id in member_ids]


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
    joined_ids: set[int] = set()
    if group and candidate_ids:
        link_stmt = select(TgGroupAccount.account_id).where(
            TgGroupAccount.tenant_id == tenant_id,
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.account_id.in_(candidate_ids),
        )
        if require_send and channel.target_type == "group":
            link_stmt = link_stmt.where(TgGroupAccount.can_send.is_(True))
        joined_ids = {int(account_id) for account_id in session.scalars(link_stmt)}
    terminal_actions = _membership_actions_by_account(session, channel.id, task_id=task_id)
    failed_ids = {
        account_id
        for account_id, action in terminal_actions.items()
        if action.status == "failed" and account_id in candidate_ids
    }
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
        .where(TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None), TgAccount.status == AccountStatus.ACTIVE.value)
        .order_by(TgAccount.health_score.desc(), TgAccount.id.asc())
    )
    mode = account_config.get("selection_mode") or "all"
    if mode == "manual":
        account_ids = [int(item) for item in account_config.get("account_ids") or []]
        if not account_ids:
            return []
        stmt = stmt.where(TgAccount.id.in_(account_ids))
    elif mode == "group":
        pool_id = int(account_config.get("account_group_id") or 0)
        pool = session.get(AccountPool, pool_id) if pool_id else None
        if not pool or pool.tenant_id != tenant_id:
            return []
        stmt = stmt.where(TgAccount.pool_id == pool.id)
    return list(session.scalars(stmt))


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
    if channel.target_type != "channel":
        return False
    return not (channel.can_send and channel.auth_status == GroupAuthStatus.AUTHORIZED.value)


def _create_missing_membership_actions(session: Session, task: Task, channel: OperationTarget, candidates: list[TgAccount], *, require_send: bool = False) -> int:
    existing = _membership_actions_by_account(session, channel.id, task_id=task.id)
    group = linked_channel_group(session, channel, create=True)
    link_stmt = select(TgGroupAccount.account_id).where(
        TgGroupAccount.tenant_id == task.tenant_id,
        TgGroupAccount.group_id == group.id,
    )
    if require_send and channel.target_type == "group":
        link_stmt = link_stmt.where(TgGroupAccount.can_send.is_(True))
    joined_ids = {int(account_id) for account_id in session.scalars(link_stmt)}
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


def _membership_pacing_config(task: Task) -> dict[str, Any]:
    config = dict(task.pacing_config or {})
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
