from __future__ import annotations

import argparse
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.task_center import Action, ExecutionAttempt
from app.services._common import _now
from app.services.task_center.direct_action_claims import (
    settle_fact_first_action_before_gateway,
    reconcile_source_pacing_states,
)

DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 500
CHANNEL_TASK_TYPES = frozenset({"channel_view", "channel_like", "channel_comment"})
ALL_HISTORICAL_TASK_TYPES = CHANNEL_TASK_TYPES | {"group_ai_chat"}


def abandon_channel_historical_backlog(
    session: Session,
    *,
    cutoff: datetime,
    apply: bool,
    batch_size: int = DEFAULT_BATCH_SIZE,
    task_ids: set[str] | None = None,
    task_types: set[str] | None = None,
) -> dict:
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=ZoneInfo("Asia/Shanghai"))

    effective_task_types = task_types if task_types is not None else CHANNEL_TASK_TYPES

    gateway_started = select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == Action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).exists()

    stmt = select(Action.id).where(
        Action.task_type.in_(effective_task_types),
        Action.status.in_(("pending", "retryable_failed")),
        Action.scheduled_at < cutoff,
        ~gateway_started,
    ).order_by(Action.scheduled_at.asc(), Action.id.asc())

    if task_ids:
        stmt = stmt.where(Action.task_id.in_(task_ids))

    action_ids = list(session.scalars(stmt))
    result = {
        "mode": "apply" if apply else "preview",
        "cutoff": cutoff.isoformat(),
        "candidate_count": len(action_ids),
        "settled_count": 0,
    }

    if not apply or not action_ids:
        return result

    settled = 0
    chunk_size = max(1, min(batch_size, MAX_BATCH_SIZE))
    for i in range(0, len(action_ids), chunk_size):
        chunk_ids = action_ids[i:i + chunk_size]
        locked = (
            select(Action)
            .where(stmt.whereclause, Action.id.in_(chunk_ids))
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        settled += _settle_batch(session, locked)
    result["settled_count"] = settled
    return result


def _settle_batch(session: Session, stmt) -> int:
    actions = list(session.scalars(stmt))
    all_state_ids: set[str] = set()
    now = _now()
    for action in actions:
        detail = (
            "历史积压AI活群动作按截止时间安全下线"
            if action.task_type == "group_ai_chat"
            else "历史积压频道动作按截止时间安全下线"
        )
        state_ids = settle_fact_first_action_before_gateway(
            session,
            action,
            now=now,
            reason_code="pacing_claim_deadline_exceeded",
            detail=detail,
        )
        all_state_ids.update(state_ids)
    if all_state_ids:
        reconcile_source_pacing_states(session, all_state_ids)
    session.commit()
    return len(actions)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview or abandon pre-cutoff channel and AI historical backlog.",
    )
    parser.add_argument("--cutoff", required=True, type=datetime.fromisoformat)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--task-id", action="append", default=[])
    parser.add_argument("--task-type", action="append", default=[])
    parser.add_argument("--include-ai-group", action="store_true")
    args = parser.parse_args()

    effective_task_types = None
    if args.task_type:
        effective_task_types = set(args.task_type)
    elif args.include_ai_group:
        effective_task_types = set(ALL_HISTORICAL_TASK_TYPES)

    with SessionLocal() as session:
        result = abandon_channel_historical_backlog(
            session,
            cutoff=args.cutoff,
            apply=args.apply,
            batch_size=args.batch_size,
            task_ids=set(args.task_id) if args.task_id else None,
            task_types=effective_task_types,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
