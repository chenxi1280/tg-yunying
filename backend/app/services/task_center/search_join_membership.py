from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, Task

from .payloads import SearchJoinMembershipPayload, SearchJoinPayload, create_search_join_membership_action


SOURCE_ACTION_TYPE = "search_join"
MEMBERSHIP_ACTION_TYPE = "search_join_membership"
MEMBERSHIP_PENDING_STATUS = "membership_pending"
MEMBERSHIP_OBSERVED_STATUS = "membership_observed"


def create_membership_child(
    session: Session,
    source: Action,
    payload: SearchJoinPayload,
    scheduled_at: datetime,
) -> Action:
    existing = membership_child_for_source(session, source)
    if existing is not None:
        return existing
    task = session.get(Task, source.task_id)
    if task is None:
        raise ValueError("search_join_membership_task_not_found")
    child_payload = _membership_payload(source, payload)
    return create_search_join_membership_action(session, task, source.account_id, scheduled_at, child_payload)


def membership_child_for_source(session: Session, source: Action) -> Action | None:
    return session.scalar(
        select(Action).where(
            Action.tenant_id == source.tenant_id,
            Action.task_id == source.task_id,
            Action.action_type == MEMBERSHIP_ACTION_TYPE,
            Action.payload["source_search_join_action_id"].as_string() == source.id,
        )
    )


def source_action_for_membership(session: Session, action: Action, payload: SearchJoinMembershipPayload) -> Action | None:
    source = session.get(Action, payload.source_search_join_action_id)
    if source is None or source.action_type != SOURCE_ACTION_TYPE:
        return None
    if source.tenant_id != action.tenant_id or source.task_id != action.task_id or source.account_id != action.account_id:
        return None
    return source


def mark_source_membership_pending(source: Action, child: Action, *, timestamp: datetime, detail: str = "") -> None:
    result = dict(source.result or {})
    result.update(
        {
            "success": True,
            "join_status": MEMBERSHIP_PENDING_STATUS,
            "target_click_observed": True,
            "target_found_at": result.get("target_found_at") or timestamp.isoformat(),
            "membership_action_id": child.id,
            "membership_pending_at": timestamp.isoformat(),
        }
    )
    if detail:
        result["membership_pending_detail"] = detail
    source.result = result


def mark_source_membership_observed(
    source: Action,
    child: Action,
    result: dict,
    *,
    observed_at: datetime,
) -> None:
    source_result = _source_result_without_pending_fields(source)
    source_result.update(_membership_result_fields(child, result))
    source_result.update(
        {
            "success": True,
            "join_status": MEMBERSHIP_OBSERVED_STATUS,
            "membership_observed": True,
            "membership_action_id": child.id,
            "membership_observed_at": observed_at.isoformat(),
        }
    )
    source.status = "success"
    source.result = source_result


def mark_source_membership_failed(
    source: Action,
    child: Action,
    result: dict,
    *,
    observed_at: datetime,
) -> None:
    source_result = _source_result_without_pending_fields(source)
    source_result.update(_membership_result_fields(child, result))
    source_result.update(
        {
            "success": False,
            "join_status": "membership_failed",
            "membership_action_id": child.id,
            "membership_failed_at": observed_at.isoformat(),
            "error_code": str(result.get("error_code") or "search_join_membership_failed"),
            "error_message": str(result.get("error_message") or result.get("detail") or "搜索目标群准入未完成"),
        }
    )
    source.status = "failed"
    source.executed_at = observed_at
    source.result = source_result


def is_join_request_pending(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("error_code") == "join_request_pending":
        return True
    detail = str(result.get("detail") or result.get("error_message") or "").lower()
    return "requested to join this chat or channel" in detail


def _membership_payload(source: Action, payload: SearchJoinPayload) -> SearchJoinMembershipPayload:
    return SearchJoinMembershipPayload(
        source_search_join_action_id=source.id,
        authorization_id=payload.authorization_id,
        session_role=payload.session_role,
        client_metadata=dict(payload.client_metadata),
        target_operation_target_id=payload.target_operation_target_id,
        target_group_id=payload.target_group_id,
        target_username=payload.target_username,
        target_title=payload.target_title,
        target_peer_id=payload.target_peer_id,
        post_join_policy=payload.post_join_policy,
        runtime_environment=dict(payload.runtime_environment),
    )


def _source_result_without_pending_fields(source: Action) -> dict:
    result = dict(source.result or {})
    for key in ("membership_pending_at", "membership_pending_detail"):
        result.pop(key, None)
    return result


def _membership_result_fields(child: Action, result: dict) -> dict:
    return {
        "target_group_id": result.get("target_group_id") or child.payload.get("target_group_id"),
        "target_peer_id": result.get("target_peer_id") or child.payload.get("target_peer_id"),
        "membership_detail": str(result.get("detail") or result.get("error_message") or ""),
    }


__all__ = [
    "MEMBERSHIP_ACTION_TYPE",
    "MEMBERSHIP_OBSERVED_STATUS",
    "MEMBERSHIP_PENDING_STATUS",
    "SOURCE_ACTION_TYPE",
    "create_membership_child",
    "is_join_request_pending",
    "mark_source_membership_failed",
    "mark_source_membership_observed",
    "mark_source_membership_pending",
    "membership_child_for_source",
    "source_action_for_membership",
]
