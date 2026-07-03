from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Action, SearchJoinLinkedTaskDispatch
from app.services._common import _now


MEMBERSHIP_OBSERVED_STATUS = "membership_observed"
DEFAULT_LINK_TYPE = "group_ai_chat"
DEFAULT_READY_STATUS = "linked_task_ready_pending"


def create_linked_dispatch_if_membership_observed(
    session: Session,
    action: Action,
    *,
    linked_task_id: str,
    activation_not_before: datetime | None = None,
    can_send_checked_at: datetime | None = None,
) -> SearchJoinLinkedTaskDispatch | None:
    if not _membership_observed(action):
        return None
    dispatch = SearchJoinLinkedTaskDispatch(
        tenant_id=action.tenant_id,
        search_join_action_id=action.id,
        source_task_id=action.task_id,
        linked_task_id=linked_task_id,
        account_id=action.account_id,
        target_group_id=_target_group_id(action),
        link_type=DEFAULT_LINK_TYPE,
        status=DEFAULT_READY_STATUS,
        block_reason="cooldown_waiting" if activation_not_before else "",
        can_send_checked_at=can_send_checked_at,
        activation_not_before=activation_not_before,
        detail={"source": "search_join_group"},
    )
    session.add(dispatch)
    session.flush()
    return dispatch


def _membership_observed(action: Action) -> bool:
    result = action.result if isinstance(action.result, dict) else {}
    return result.get("join_status") == MEMBERSHIP_OBSERVED_STATUS


def _target_group_id(action: Action) -> int | None:
    result = action.result if isinstance(action.result, dict) else {}
    payload = action.payload if isinstance(action.payload, dict) else {}
    return int(result.get("target_group_id") or payload.get("target_group_id") or 0) or None


__all__ = ["create_linked_dispatch_if_membership_observed"]
