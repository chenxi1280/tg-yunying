from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date

from app.models import Task, TaskParticipationUnitPlan

from .engagement_binding import UNIFIED_ENGAGEMENT_CONTRACT_VERSION


POLICY_REVISION = "group_ai_daily_quantity_uniform_v1"


@dataclass(frozen=True)
class DailyQuantityDecision:
    configured_target: int
    sampled_jitter_bps: int
    raw_target: int
    effective_target: int
    seed: str
    policy_revision: str


def group_ai_daily_quantity(
    task: Task,
    target_date: date,
    *,
    required_account_count: int,
    participation_plan: TaskParticipationUnitPlan | None,
) -> DailyQuantityDecision:
    config = task.type_config or {}
    configured = max(1, int(config.get("daily_message_target") or 1))
    if config.get("engagement_contract_version") != UNIFIED_ENGAGEMENT_CONTRACT_VERSION:
        return DailyQuantityDecision(
            configured, 0, configured,
            max(configured, required_account_count), "", "legacy_fixed_v0",
        )
    jitter_limit = int(config.get("daily_target_jitter_bps") or 0)
    seed = _seed(task, target_date, participation_plan)
    sampled = _signed_jitter(seed, jitter_limit)
    raw = max(1, _round_half_away(configured * (10000 + sampled), 10000))
    return DailyQuantityDecision(
        configured,
        sampled,
        raw,
        max(raw, required_account_count),
        seed,
        POLICY_REVISION,
    )


def _seed(
    task: Task,
    target_date: date,
    participation_plan: TaskParticipationUnitPlan | None,
) -> str:
    identity = {
        "tenant_id": task.tenant_id,
        "task_id": task.id,
        "lifecycle_epoch": task.task_lifecycle_epoch,
        "target_date": target_date.isoformat(),
        "adapter": "group_ai_chat",
        "policy": POLICY_REVISION,
        "participation_hash": (
            participation_plan.selection_hash if participation_plan else ""
        ),
    }
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _signed_jitter(seed: str, limit_bps: int) -> int:
    if limit_bps <= 0:
        return 0
    sample = int(hashlib.sha256(f"{seed}:daily_quantity".encode()).hexdigest()[:16], 16)
    scale = 1 << 64
    return _round_half_away((2 * sample - scale) * limit_bps, scale)


def _round_half_away(numerator: int, denominator: int) -> int:
    if numerator >= 0:
        return (numerator + denominator // 2) // denominator
    return -((-numerator + denominator // 2) // denominator)
