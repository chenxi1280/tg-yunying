from __future__ import annotations

import json

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import AccountPool, Action, MessageTask, TaskStatus, TgAccount

from ._common import audit
from .account_usage_policy import AccountUsageSyncSummary, sync_account_usage
from .dedicated_account_pools import RANK_DEBOOST_POOL_KEY

UNSTARTED_ACTION_STATUSES = frozenset({"pending", "retryable_failed"})
UNSTARTED_MESSAGE_STATUSES = frozenset(
    {
        TaskStatus.DRAFT.value,
        TaskStatus.PENDING_REVIEW.value,
        TaskStatus.APPROVED.value,
        TaskStatus.QUEUED.value,
    }
)
USAGE_MIGRATION_REASON = "账号用途迁移，取消尚未开始的旧用途任务"


def locked_account_and_pool(
    session: Session,
    account_id: int,
    pool_id: int,
) -> tuple[TgAccount, AccountPool]:
    account = session.scalar(select(TgAccount).where(TgAccount.id == account_id).with_for_update())
    pool = session.scalar(select(AccountPool).where(AccountPool.id == pool_id).with_for_update())
    if not account or account.deleted_at is not None or not pool or account.tenant_id != pool.tenant_id:
        raise ValueError("account or pool not found")
    return account, pool


def migrate_account_usage(
    session: Session,
    account: TgAccount,
    pool: AccountPool,
    *,
    actor: str,
    audit_action: str,
) -> TgAccount:
    summary = sync_account_usage(session, account, pool, actor)
    action_count = _cancel_incompatible_actions(session, summary)
    message_count = _cancel_incompatible_messages(session, summary)
    detail = _migration_audit_detail(summary, action_count, message_count)
    audit(
        session,
        tenant_id=account.tenant_id,
        actor=actor,
        action=audit_action,
        target_type="tg_account",
        target_id=str(account.id),
        detail=detail,
    )
    from app.services.task_center.account_scope import emit_account_eligibility_event

    emit_account_eligibility_event(session, account.id, "account_usage_changed")
    session.commit()
    session.refresh(account)
    return account


def _cancel_incompatible_actions(session: Session, summary: AccountUsageSyncSummary) -> int:
    action_filter = _migration_action_filter(summary)
    if action_filter is None:
        return 0
    actions = session.scalars(
        select(Action)
        .where(
            Action.tenant_id == summary.tenant_id,
            Action.account_id == summary.account_id,
            Action.status.in_(UNSTARTED_ACTION_STATUSES),
            action_filter,
        )
        .with_for_update()
    ).all()
    for action in actions:
        action.status = "skipped"
        action.result = _cancelled_action_result(action.result)
    return len(actions)


def _cancelled_action_result(result: dict | None) -> dict:
    return {
        **(result or {}),
        "success": False,
        "skip_reason": "account_usage_migrated",
        "error_message": USAGE_MIGRATION_REASON,
    }


def _migration_action_filter(summary: AccountUsageSyncSummary):
    if summary.previous_usage == "normal" and summary.usage == RANK_DEBOOST_POOL_KEY:
        return Action.action_type != "search_rank_deboost"
    if summary.previous_usage == RANK_DEBOOST_POOL_KEY and summary.usage != RANK_DEBOOST_POOL_KEY:
        return Action.action_type == "search_rank_deboost"
    return None


def _cancel_incompatible_messages(session: Session, summary: AccountUsageSyncSummary) -> int:
    if summary.previous_usage != "normal" or summary.usage != RANK_DEBOOST_POOL_KEY:
        return 0
    tasks = session.scalars(
        select(MessageTask)
        .where(
            MessageTask.tenant_id == summary.tenant_id,
            MessageTask.status.in_(UNSTARTED_MESSAGE_STATUSES),
            or_(MessageTask.account_id == summary.account_id, MessageTask.preferred_account_id == summary.account_id),
        )
        .with_for_update()
    ).all()
    for task in tasks:
        task.status = TaskStatus.CANCELLED.value
        task.failure_type = "account_usage_migrated"
        task.failure_detail = USAGE_MIGRATION_REASON
    return len(tasks)


def _migration_audit_detail(summary: AccountUsageSyncSummary, action_count: int, message_count: int) -> str:
    return json.dumps(
        {
            "previous_usage": summary.previous_usage,
            "usage": summary.usage,
            "previous_pool_id": summary.previous_pool_id,
            "target_pool_id": summary.target_pool_id,
            "cancelled_actions": action_count,
            "cancelled_message_tasks": message_count,
        },
        ensure_ascii=False,
    )


__all__ = ["locked_account_and_pool", "migrate_account_usage"]
