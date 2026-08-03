from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ContentMixCycle,
    ContentMixCycleSlot,
    TaskGroupDailyTarget,
)


def reply_requirement_for_plan(
    session: Session,
    *,
    turn_count: int,
    config: dict,
    daily_group_target_id: str,
) -> int:
    configured = min(turn_count, int(config.get("reply_min_per_round") or 0))
    if (
        config.get("account_coverage_mode") != "all_accounts_daily"
        or not daily_group_target_id
        or configured <= 0
    ):
        return configured
    target = session.get(TaskGroupDailyTarget, daily_group_target_id)
    if target is None or not target.task_day_ledger_id:
        raise RuntimeError("daily_group_target_ledger_missing")
    prior_total, prior_reply = _frozen_slot_counts(
        session,
        task_id=target.task_id,
        ledger_id=target.task_day_ledger_id,
    )
    return cumulative_reply_requirement(
        prior_total=prior_total,
        prior_reply=prior_reply,
        batch_total=turn_count,
        round_total=int(config.get("messages_per_round") or 0),
        round_reply=int(config.get("reply_min_per_round") or 0),
    )


def _frozen_slot_counts(
    session: Session,
    *,
    task_id: str,
    ledger_id: str,
) -> tuple[int, int]:
    total, reply = session.execute(
        select(
            func.count(ContentMixCycleSlot.id),
            func.count(ContentMixCycleSlot.id).filter(
                ContentMixCycleSlot.relation_kind == "reply",
            ),
        )
        .join(ContentMixCycle, ContentMixCycle.id == ContentMixCycleSlot.cycle_id)
        .where(
            ContentMixCycle.task_id == task_id,
            ContentMixCycle.task_day_ledger_id == ledger_id,
        )
    ).one()
    return int(total or 0), int(reply or 0)


def cumulative_reply_requirement(
    *,
    prior_total: int,
    prior_reply: int,
    batch_total: int,
    round_total: int,
    round_reply: int,
) -> int:
    if round_total <= 0 or round_reply < 0 or round_reply > round_total:
        raise ValueError("content_mix_reply_ratio_invalid")
    desired = ((prior_total + batch_total) * round_reply) // round_total
    return max(0, min(batch_total, desired - prior_reply))


__all__ = ["cumulative_reply_requirement", "reply_requirement_for_plan"]
