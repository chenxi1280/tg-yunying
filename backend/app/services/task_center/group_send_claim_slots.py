"""Claim-time protection for legacy group send slots."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, TgGroup
from app.timezone import as_beijing

from .group_send_limits import SEND_LIMIT_MODE_LEGACY_GROUP_SLOT


INFLIGHT_SEND_STATUSES = ("claiming", "executing")


def filter_ready_group_send_actions(session: Session, actions: list[Action], now: datetime) -> list[Action]:
    """Remove legacy-group sends that are already in flight or await a reserved slot."""
    legacy_groups = _legacy_groups(session, _group_ids(actions))
    return _eligible_actions(actions, legacy_groups, _inflight_group_ids(session), now)


def lock_eligible_group_send_actions(session: Session, actions: list[Action], now: datetime) -> list[Action]:
    """Lock legacy groups and keep the first planned Action for each available group."""
    group_ids = _group_ids(actions)
    legacy_groups = _legacy_groups(session, group_ids)
    locked_groups = _locked_legacy_groups(session, legacy_groups)
    unavailable = set(legacy_groups).difference(locked_groups)
    return _eligible_actions(actions, locked_groups, _inflight_group_ids(session), now, unavailable)


def _eligible_actions(
    actions: list[Action],
    legacy_groups: dict[int, TgGroup],
    inflight_group_ids: set[int],
    now: datetime,
    unavailable_group_ids: set[int] | None = None,
) -> list[Action]:
    unavailable = unavailable_group_ids or set()
    selected: list[Action] = []
    selected_groups: set[int] = set()
    for action in actions:
        group_id = _group_id(action)
        if group_id not in legacy_groups:
            selected.append(action)
            continue
        if group_id in unavailable or group_id in inflight_group_ids or _slot_is_future(legacy_groups[group_id], now):
            continue
        if group_id not in selected_groups:
            selected.append(action)
            selected_groups.add(group_id)
    return selected


def _group_ids(actions: list[Action]) -> set[int]:
    return {group_id for action in actions if (group_id := _group_id(action)) is not None}


def _group_id(action: Action) -> int | None:
    if action.action_type != "send_message":
        return None
    value = (action.payload or {}).get("group_id")
    return int(value) if isinstance(value, int) or (isinstance(value, str) and value.isdigit()) else None


def _legacy_groups(session: Session, group_ids: set[int]) -> dict[int, TgGroup]:
    if not group_ids:
        return {}
    rows = session.scalars(
        select(TgGroup).where(TgGroup.id.in_(group_ids), TgGroup.send_limit_mode == SEND_LIMIT_MODE_LEGACY_GROUP_SLOT)
    )
    return {int(group.id): group for group in rows}


def _locked_legacy_groups(session: Session, groups: dict[int, TgGroup]) -> dict[int, TgGroup]:
    if not groups:
        return {}
    statement = select(TgGroup).where(TgGroup.id.in_(groups))
    if session.bind and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update(skip_locked=True)
    return {int(group.id): group for group in session.scalars(statement)}


def _inflight_group_ids(session: Session) -> set[int]:
    actions = session.scalars(
        select(Action).where(Action.action_type == "send_message", Action.status.in_(INFLIGHT_SEND_STATUSES))
    )
    return {group_id for action in actions if (group_id := _group_id(action)) is not None}


def _slot_is_future(group: TgGroup, now: datetime) -> bool:
    next_slot = as_beijing(group.next_group_send_slot_at)
    return next_slot is not None and next_slot > as_beijing(now)
