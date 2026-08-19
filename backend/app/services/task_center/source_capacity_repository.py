from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import SourcePacingCapacityPlan


class CapacityScopeLike(Protocol):
    tenant_id: int
    pacing_domain: str
    source_key_hash: str
    window_start_at: datetime
    window_end_at: datetime
    policy_version_id: str


def lock_source_capacity(session: Session, scope: CapacityScopeLike) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    payload = ":".join((
        str(scope.tenant_id),
        scope.pacing_domain,
        scope.source_key_hash,
        scope.policy_version_id,
    ))
    lock_key = int.from_bytes(
        hashlib.sha256(payload.encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=True,
    )
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )


def overlapping_scope_window(
    session: Session,
    scope: CapacityScopeLike,
) -> tuple[datetime, datetime]:
    window_start = scope.window_start_at
    window_end = scope.window_end_at
    while True:
        plans = _overlapping_capacity_plans(session, scope, window_start, window_end)
        if not plans:
            return window_start, window_end
        next_start = min(window_start, *(plan.window_start_at for plan in plans))
        next_end = max(window_end, *(plan.window_end_at for plan in plans))
        if (next_start, next_end) == (window_start, window_end):
            return window_start, window_end
        window_start, window_end = next_start, next_end


def latest_capacity_component_plans(
    session: Session,
    scope: CapacityScopeLike,
) -> tuple[SourcePacingCapacityPlan, ...]:
    plans = _overlapping_capacity_plans(
        session,
        scope,
        scope.window_start_at,
        scope.window_end_at,
    )
    components: list[list[SourcePacingCapacityPlan]] = []
    component_end: datetime | None = None
    for plan in sorted(plans, key=lambda item: (item.window_start_at, item.window_end_at)):
        if component_end is None or plan.window_start_at >= component_end:
            components.append([plan])
            component_end = plan.window_end_at
            continue
        components[-1].append(plan)
        component_end = max(component_end, plan.window_end_at)
    return tuple(max(component, key=_plan_recency) for component in components)


def _plan_recency(plan: SourcePacingCapacityPlan) -> tuple:
    return plan.created_at, int(plan.revision or 0), plan.id


def source_capacity_plan(
    session: Session,
    scope: CapacityScopeLike,
    *,
    plan_hash: str | None = None,
) -> SourcePacingCapacityPlan | None:
    statement = select(SourcePacingCapacityPlan).where(
        SourcePacingCapacityPlan.tenant_id == scope.tenant_id,
        SourcePacingCapacityPlan.pacing_domain == scope.pacing_domain,
        SourcePacingCapacityPlan.source_key_hash == scope.source_key_hash,
        SourcePacingCapacityPlan.window_start_at == scope.window_start_at,
        SourcePacingCapacityPlan.window_end_at == scope.window_end_at,
        SourcePacingCapacityPlan.policy_version_id == scope.policy_version_id,
    )
    if plan_hash is not None:
        statement = statement.where(SourcePacingCapacityPlan.plan_hash == plan_hash)
    return session.scalar(
        statement.order_by(SourcePacingCapacityPlan.revision.desc()).limit(1)
    )


def _overlapping_capacity_plans(
    session: Session,
    scope: CapacityScopeLike,
    window_start: datetime,
    window_end: datetime,
) -> list[SourcePacingCapacityPlan]:
    return list(session.scalars(
        select(SourcePacingCapacityPlan).where(
            SourcePacingCapacityPlan.tenant_id == scope.tenant_id,
            SourcePacingCapacityPlan.pacing_domain == scope.pacing_domain,
            SourcePacingCapacityPlan.source_key_hash == scope.source_key_hash,
            SourcePacingCapacityPlan.policy_version_id == scope.policy_version_id,
            SourcePacingCapacityPlan.state == "frozen",
            SourcePacingCapacityPlan.window_start_at < window_end,
            SourcePacingCapacityPlan.window_end_at > window_start,
        ).order_by(
            SourcePacingCapacityPlan.created_at.desc(),
            SourcePacingCapacityPlan.id.desc(),
        )
    ))


__all__ = [
    "latest_capacity_component_plans",
    "lock_source_capacity",
    "overlapping_scope_window",
    "source_capacity_plan",
]
