from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.services._common import _now
from app.timezone import BEIJING_TZ

from .account_pacing_timeline import account_timeline_points, earliest_available_time
from .engagement_pair_pacing import typed_account_policy_not_before


def wall_time(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(BEIJING_TZ).replace(tzinfo=None)


def account_policy_not_before(
    session: Session,
    account_id: int,
    *,
    tenant_id: int,
    now_value: datetime | None = None,
    deadline_at: datetime | None = None,
    exclude_action_id: str | None = None,
    exclude_slot_key: str | None = None,
    include_planned: bool = True,
) -> datetime | None:
    desired_at = wall_time(now_value or _now())
    if desired_at is None:
        return None
    gap = timedelta(
        seconds=max(1, int(get_settings().account_soft_pacing_min_gap_seconds))
    )
    points = account_timeline_points(
        session,
        tenant_id,
        account_id,
        desired_at=desired_at,
        gap=gap,
        deadline_at=deadline_at,
        exclude_action_id=exclude_action_id,
        exclude_slot_key=exclude_slot_key,
        include_planned=include_planned,
    )
    return earliest_available_time(desired_at, points, gap)


def task_policy_not_before(
    session: Session,
    task_id: str,
    *,
    tenant_id: int,
    desired_at: datetime,
    gap: timedelta,
    deadline_at: datetime | None = None,
    exclude_action_id: str | None = None,
    exclude_slot_key: str | None = None,
    include_planned: bool = True,
) -> datetime | None:
    points = account_timeline_points(
        session,
        tenant_id,
        None,
        desired_at=desired_at,
        gap=gap,
        deadline_at=deadline_at,
        exclude_action_id=exclude_action_id,
        exclude_slot_key=exclude_slot_key,
        task_id=task_id,
        include_planned=include_planned,
    )
    return earliest_available_time(desired_at, points, gap)


def account_not_before(
    session: Session,
    *,
    tenant_id: int,
    account_id: int,
    action_class: str,
    use_pair_policy: bool,
    now_value: datetime,
    deadline_at: datetime | None,
    exclude_action_id: str | None,
    exclude_slot_key: str | None,
    include_planned: bool,
) -> datetime | None:
    if not use_pair_policy:
        return account_policy_not_before(
            session,
            account_id,
            tenant_id=tenant_id,
            now_value=now_value,
            deadline_at=deadline_at,
            exclude_action_id=exclude_action_id,
            exclude_slot_key=exclude_slot_key,
            include_planned=include_planned,
        )
    gap = timedelta(
        seconds=max(1, int(get_settings().account_soft_pacing_min_gap_seconds))
    )
    return typed_account_policy_not_before(
        session,
        tenant_id=tenant_id,
        account_id=account_id,
        candidate_class=action_class,
        desired_at=now_value,
        default_gap=gap,
        deadline_at=deadline_at,
        exclude_action_id=exclude_action_id,
        exclude_slot_key=exclude_slot_key,
        include_planned=include_planned,
    )


__all__ = ["account_not_before", "account_policy_not_before", "task_policy_not_before"]
