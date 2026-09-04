from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import (
    ChannelMessage,
    ChannelMessageSourceRevision,
    Task,
    TaskDayLedger,
    TaskParticipationUnitPlan,
)

from .engagement_participation import ensure_source_participation_plan
from .engagement_participation import apply_journey_participation_selection
from .engagement_source_journey import JourneyDemand, register_source_journey_demand


POLICY_REVISION = "channel_comment_business_grounding_v1_2"
DEFAULT_MIN_BPS = 5500
DEFAULT_MAX_BPS = 6500
DEFAULT_BUSINESS_MAX_COMMENTS = 80
DEFAULT_PLANNED_FALLBACK_MAX_BPS = 2000


@dataclass(frozen=True)
class CommentParticipationDecision:
    ranked_accounts: list
    sampled_bps: int
    rounded_count: int
    required_count: int
    business_max: int
    realized_bps: int | None
    integer_quantization_adjustment: bool
    source_plan: TaskParticipationUnitPlan | None
    seed: str
    journey_plan_id: str = ""


def prepare_comment_participation(
    session: Session,
    task: Task,
    message: ChannelMessage,
    *,
    source: ChannelMessageSourceRevision,
    ledger: TaskDayLedger | None,
    accounts: list,
    business_max: int,
) -> CommentParticipationDecision:
    seed = _seed(task, message)
    ranked = sorted(accounts, key=lambda account: _rank(seed, account.id))
    minimum, maximum = _ratio_range(task)
    sampled = _sampled_bps(seed, minimum, maximum)
    rounded = _round_count(len(ranked), sampled)
    required = min(rounded, business_max)
    source_plan = _source_plan(
        session, task, source, ledger=ledger, ranked=ranked, required=required
    )
    source_plan, journey_plan_id = _apply_source_journey(
        session,
        task,
        source,
        ledger=ledger,
        plan=source_plan,
        ranked=ranked,
        required=required,
    )
    ranked = _selected_first(ranked, source_plan)
    realized = _realized_bps(len(ranked), required)
    return CommentParticipationDecision(
        ranked_accounts=ranked,
        sampled_bps=sampled,
        rounded_count=rounded,
        required_count=required,
        business_max=business_max,
        realized_bps=realized,
        integer_quantization_adjustment=(
            realized is not None and not minimum <= realized <= maximum
        ),
        source_plan=source_plan,
        seed=seed,
        journey_plan_id=journey_plan_id,
    )


def comment_participation_contract_fields(
    decision: CommentParticipationDecision,
    *,
    ledger: TaskDayLedger | None,
    daily_plan: TaskParticipationUnitPlan | None,
) -> dict:
    return {
        "task_day_ledger_id": ledger.id if ledger is not None else None,
        "daily_participation_plan_id": daily_plan.id if daily_plan else None,
        "source_participation_plan_id": (
            decision.source_plan.id if decision.source_plan else None
        ),
        "eligible_account_count": len(decision.ranked_accounts),
        "eligibility_snapshot_state": (
            "ready" if decision.ranked_accounts else "no_eligible_accounts"
        ),
        "participation_seed": decision.seed,
        "effective_participation_bps": decision.sampled_bps,
        "rounded_required_distinct_account_count": decision.rounded_count,
        "realized_participation_bps": decision.realized_bps,
        "integer_quantization_adjustment": (
            decision.integer_quantization_adjustment
        ),
        "uncapped_required_distinct_account_count": decision.rounded_count,
        "business_max_comments_per_message": decision.business_max,
        "business_cap_state": (
            "business_cap_adjusted"
            if decision.required_count < decision.rounded_count
            else "not_adjusted"
        ),
        "required_distinct_account_count": decision.required_count,
    }


def business_max_comments(task: Task) -> int:
    config = task.type_config or {}
    value = config.get("business_max_comments_per_message")
    if value is not None:
        return int(value)
    target = config.get("target_comments_per_message")
    return max(DEFAULT_BUSINESS_MAX_COMMENTS, int(target or 0))


def planned_fallback_max_bps(task: Task) -> int:
    value = (task.type_config or {}).get("planned_fallback_max_bps")
    return DEFAULT_PLANNED_FALLBACK_MAX_BPS if value is None else int(value)


def account_ids_hash(accounts: list) -> str:
    payload = json.dumps(
        [int(account.id) for account in accounts], separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _source_plan(
    session: Session,
    task: Task,
    source: ChannelMessageSourceRevision,
    *,
    ledger: TaskDayLedger | None,
    ranked: list,
    required: int,
) -> TaskParticipationUnitPlan | None:
    if ledger is None:
        return None
    identity = (
        f"channel:{source.channel_target_id}:"
        f"message:{source.source_remote_message_id}:revision:{source.id}"
    )
    return ensure_source_participation_plan(
        session,
        task,
        ledger,
        source_identity=identity,
        required_count=required,
        eligible_account_ids=[int(account.id) for account in ranked],
        rolling_days=int((task.type_config or {}).get("rolling_window_days") or 3),
    )


def _apply_source_journey(
    session: Session,
    task: Task,
    source: ChannelMessageSourceRevision,
    *,
    ledger: TaskDayLedger | None,
    plan: TaskParticipationUnitPlan | None,
    ranked: list,
    required: int,
) -> tuple[TaskParticipationUnitPlan | None, str]:
    if ledger is None or plan is None:
        return plan, ""
    journey = register_source_journey_demand(
        session,
        source,
        task_day=ledger.obligation_local_date,
        demand=JourneyDemand(
            task.id,
            "authored_comment",
            required,
            tuple(int(account.id) for account in ranked),
            tuple(int(item) for item in plan.selected_account_ids or []),
        ),
    )
    if not journey.achievable:
        raise ValueError("cross_adapter_journey_unachievable")
    selected = journey.account_ids_by_task_action.get(
        (task.id, "authored_comment"), ()
    )
    successor = apply_journey_participation_selection(
        session,
        task,
        plan,
        selected_account_ids=selected,
        journey_plan_id=journey.plan.id,
    )
    return successor, journey.plan.id


def _selected_first(
    ranked: list, plan: TaskParticipationUnitPlan | None
) -> list:
    if plan is None:
        return ranked
    position = {
        int(account_id): index
        for index, account_id in enumerate(plan.selected_account_ids or [])
    }
    fallback = len(position)
    return sorted(
        ranked,
        key=lambda account: (position.get(int(account.id), fallback), account.id),
    )


def _seed(task: Task, message: ChannelMessage) -> str:
    return f"{task.tenant_id}:{task.id}:{message.id}:1:{POLICY_REVISION}"


def _rank(seed: str, account_id: int) -> str:
    return hashlib.sha256(f"{seed}:{account_id}".encode()).hexdigest()


def _sampled_bps(seed: str, minimum: int, maximum: int) -> int:
    digest = hashlib.sha256(seed.encode()).digest()
    return minimum + int.from_bytes(digest[:4], "big") % (maximum - minimum + 1)


def _ratio_range(task: Task) -> tuple[int, int]:
    config = task.type_config or {}
    minimum = int(config.get("account_ratio_min_bps") or DEFAULT_MIN_BPS)
    maximum = int(config.get("account_ratio_max_bps") or DEFAULT_MAX_BPS)
    if minimum > maximum:
        raise ValueError("account_ratio_min_bps 不能大于 account_ratio_max_bps")
    return minimum, maximum


def _round_count(eligible_count: int, target_bps: int) -> int:
    if eligible_count <= 0:
        return 0
    return min(eligible_count, (eligible_count * target_bps + 5000) // 10000)


def _realized_bps(eligible_count: int, selected_count: int) -> int | None:
    if eligible_count <= 0:
        return None
    return (selected_count * 10000 + eligible_count // 2) // eligible_count
