from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Action, FulfillmentRemoteFact, Task
from app.services._common import _now

from .datetime_compat import is_after_or_equal


WAITING_PARENT_REMOTE_FACT = "waiting_parent_remote_fact"
PARENT_TERMINAL_STATUSES = frozenset({"failed", "skipped", "cancelled"})
CHAIN_CONTEXT_MODES = frozenset({"bootstrap", "idle_continuation", "silence"})


def link_existing_dialogue_chain(
    task: Task,
    actions: list[Action],
    *,
    context_mode: str,
) -> bool:
    config = dict(task.type_config or {})
    if not config.get("ai_dialogue_chain_enabled") or context_mode not in CHAIN_CONTEXT_MODES:
        return False
    pair = _eligible_pair(actions, minimum_gap=_minimum_group_gap())
    if pair is None:
        return False
    parent, child = pair
    chain_id = str(uuid4())
    parent.payload = {
        **dict(parent.payload or {}),
        "dialogue_chain_id": chain_id,
        "dialogue_chain_role": "parent",
        "dialogue_chain_state": "parent_planned",
        "dialogue_child_action_id": child.id,
    }
    child.payload = {
        **dict(child.payload or {}),
        "ai_generation_status": WAITING_PARENT_REMOTE_FACT,
        "dialogue_chain_id": chain_id,
        "dialogue_chain_role": "child",
        "dialogue_chain_state": WAITING_PARENT_REMOTE_FACT,
        "dialogue_parent_action_id": parent.id,
        "dialogue_parent_obligation_id": str(parent.obligation_id or ""),
    }
    child.result = {
        **dict(child.result or {}),
        "generation_stage": WAITING_PARENT_REMOTE_FACT,
        "generation_outcome": "pending",
    }
    return True


def resolve_waiting_dialogue_dependencies(session: Session, *, limit: int) -> int:
    children = list(session.scalars(
        select(Action).where(
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status == "pending",
            Action.payload["dialogue_chain_state"].as_string() == WAITING_PARENT_REMOTE_FACT,
        ).order_by(Action.scheduled_at, Action.id).limit(max(1, limit))
    ))
    return sum(int(_resolve_child(session, child)) for child in children)


def _resolve_child(session: Session, child: Action) -> bool:
    payload = dict(child.payload or {})
    parent = session.get(Action, str(payload.get("dialogue_parent_action_id") or ""))
    if parent is None or parent.status in PARENT_TERMINAL_STATUSES:
        _return_to_independent_slot(child, parent_status=parent.status if parent else "missing")
        return True
    remote_id = _typed_remote_message_id(session, parent.id)
    if remote_id is not None:
        _bind_parent_remote_fact(child, parent, remote_id)
        return True
    if _deadline_reached(child):
        _return_to_independent_slot(child, parent_status="remote_fact_deadline")
        return True
    return False


def _eligible_pair(
    actions: list[Action],
    *,
    minimum_gap: timedelta,
) -> tuple[Action, Action] | None:
    candidates = sorted(
        (action for action in actions if _chain_candidate(action)),
        key=lambda action: (action.scheduled_at, action.id),
    )
    for parent in candidates:
        for child in candidates:
            if _pair_allowed(parent, child, minimum_gap=minimum_gap):
                return parent, child
    return None


def _chain_candidate(action: Action) -> bool:
    payload = dict(action.payload or {})
    return bool(
        action.status == "pending"
        and action.account_id
        and str(payload.get("ai_generation_status") or "") == "pending"
        and not payload.get("reply_to_message_id")
        and not payload.get("dialogue_chain_id")
    )


def _pair_allowed(parent: Action, child: Action, *, minimum_gap: timedelta) -> bool:
    if parent.id == child.id or parent.account_id == child.account_id:
        return False
    if parent.obligation_id == child.obligation_id:
        return False
    parent_group = int(dict(parent.payload or {}).get("group_id") or 0)
    child_group = int(dict(child.payload or {}).get("group_id") or 0)
    return bool(
        parent_group > 0
        and parent_group == child_group
        and child.scheduled_at >= parent.scheduled_at + minimum_gap
    )


def _typed_remote_message_id(session: Session, parent_action_id: str) -> int | None:
    fact = session.scalar(
        select(FulfillmentRemoteFact).where(
            FulfillmentRemoteFact.action_id == parent_action_id,
            FulfillmentRemoteFact.fact_kind == "remote_message_observed",
        ).order_by(FulfillmentRemoteFact.observed_at.desc(), FulfillmentRemoteFact.fact_id.desc())
    )
    raw = str(dict(fact.outcome or {}).get("remote_message_id") or "") if fact else ""
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def _bind_parent_remote_fact(child: Action, parent: Action, remote_id: int) -> None:
    payload = dict(child.payload or {})
    parent_text = str(dict(parent.payload or {}).get("message_text") or "").strip()
    payload.update({
        "ai_generation_status": "pending",
        "dialogue_chain_state": "parent_remote_fact_bound",
        "reply_to_message_id": remote_id,
        "reply_target_preview": parent_text[:160],
        "reply_target_source": "dialogue_parent_remote_fact",
        "chat_mode": "reply",
        "generation_source": "dialogue_chain",
        "ai_generation_history": f"上一条群消息：{parent_text}",
    })
    child.payload = payload
    child.result = {
        **dict(child.result or {}),
        "generation_stage": "parent_remote_fact_bound",
        "generation_outcome": "ready_for_generation",
        "dialogue_parent_remote_message_id": str(remote_id),
    }


def _return_to_independent_slot(child: Action, *, parent_status: str) -> None:
    payload = dict(child.payload or {})
    payload.update({
        "ai_generation_status": "pending",
        "dialogue_chain_state": "parent_failed_independent",
        "reply_to_message_id": None,
        "reply_target_label": "",
        "reply_target_author": "",
        "reply_target_preview": "",
        "reply_target_source": "",
    })
    child.payload = payload
    child.result = {
        **dict(child.result or {}),
        "generation_stage": "parent_failed_independent",
        "generation_outcome": "ready_for_generation",
        "dialogue_parent_status": parent_status,
    }


def _deadline_reached(child: Action) -> bool:
    payload = dict(child.payload or {})
    raw = payload.get("obligation_deadline_at") or payload.get("deadline_at")
    if not raw:
        return False
    deadline = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return is_after_or_equal(_now(), deadline)


def _minimum_group_gap() -> timedelta:
    seconds = max(1, int(get_settings().ai_group_send_pacing_min_gap_seconds))
    return timedelta(seconds=seconds)


__all__ = [
    "WAITING_PARENT_REMOTE_FACT",
    "link_existing_dialogue_chain",
    "resolve_waiting_dialogue_dependencies",
]
