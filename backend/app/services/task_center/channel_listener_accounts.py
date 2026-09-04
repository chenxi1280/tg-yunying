from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, FulfillmentRemoteFact, Task, TgAccount
from app.services._common import _now

from .account_pool import select_task_accounts


CHANNEL_TASK_TYPES = frozenset({"channel_comment", "channel_like", "channel_view"})
RECENT_READER_FACT_WINDOW_HOURS = 72
RECENT_READER_CANDIDATE_LIMIT = 32


def select_channel_listener_accounts(
    session: Session,
    task: Task,
    *,
    channel_target_id: int,
    fallback_limit: int,
) -> list[TgAccount]:
    recent_ids = _recent_target_account_ids(
        session,
        task,
        channel_target_id=channel_target_id,
    )
    recent = _eligible_recent_accounts(session, task, recent_ids)
    fallback = select_task_accounts(
        session,
        task.tenant_id,
        task.account_config or {},
        limit=fallback_limit,
        enforce_max_concurrent=False,
        enforce_capacity=False,
    )
    return _unique_accounts([*recent, *fallback])


def _recent_target_account_ids(
    session: Session,
    task: Task,
    *,
    channel_target_id: int,
) -> list[int]:
    cutoff = _now() - timedelta(hours=RECENT_READER_FACT_WINDOW_HOURS)
    rows = session.execute(
        select(Action.account_id, func.max(FulfillmentRemoteFact.observed_at).label("last_fact_at"))
        .join(FulfillmentRemoteFact, FulfillmentRemoteFact.action_id == Action.id)
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_type.in_(CHANNEL_TASK_TYPES),
            Action.account_id.is_not(None),
            Action.payload["channel_target_id"].as_integer() == channel_target_id,
            FulfillmentRemoteFact.tenant_id == task.tenant_id,
            FulfillmentRemoteFact.fact_kind.in_(
                ("remote_message_observed", "reaction_observed", "view_observed")
            ),
            FulfillmentRemoteFact.observed_at >= cutoff,
        )
        .group_by(Action.account_id)
        .order_by(func.max(FulfillmentRemoteFact.observed_at).desc(), Action.account_id)
        .limit(RECENT_READER_CANDIDATE_LIMIT)
    )
    return [int(account_id) for account_id, _ in rows if account_id is not None]


def _eligible_recent_accounts(
    session: Session,
    task: Task,
    account_ids: list[int],
) -> list[TgAccount]:
    if not account_ids:
        return []
    return select_task_accounts(
        session,
        task.tenant_id,
        task.account_config or {},
        limit=len(account_ids),
        enforce_max_concurrent=False,
        enforce_capacity=False,
        candidate_account_ids=account_ids,
        scan_all_candidates=True,
    )


def _unique_accounts(accounts: list[TgAccount]) -> list[TgAccount]:
    unique: dict[int, TgAccount] = {}
    for account in accounts:
        unique.setdefault(int(account.id), account)
    return list(unique.values())


__all__ = ["select_channel_listener_accounts"]
