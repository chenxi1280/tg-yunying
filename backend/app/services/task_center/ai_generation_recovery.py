from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Action
from app.services._common import _now

from .ai_generation_commit import commit_generation_action, load_generation_batch
from .ai_generation_state import generation_result_cache, mark_attempt_outcome
from .runtime_resources import _release_runtime_resources


def persist_generation_unknown(
    session: Session,
    request,
    contents: list[str],
    *,
    tokens: int,
    attempt_id: str,
) -> None:
    batch = load_generation_batch(session, request)
    with session.no_autoflush:
        for index, ((action, payload), content) in enumerate(zip(batch, contents, strict=False)):
            data = payload.model_dump(mode="json")
            data["ai_generation_status"] = "ai_result_persist_unknown"
            data["ai_generation_result_cache"] = generation_result_cache(
                content,
                int(tokens or 0) if index == 0 else 0,
                attempt_id,
            )
            mark_attempt_outcome(
                data,
                attempt_id,
                "ai_result_persist_unknown",
                timestamp=_now(),
            )
            _reset_action_for_recovery(action, data)
            commit_generation_action(session, request, action)


def _reset_action_for_recovery(
    action: Action,
    data: dict,
    *,
    clear_claim: bool = True,
) -> None:
    action.payload = data
    action.status = "pending"
    if clear_claim:
        action.claim_owner = ""
        action.claim_token = ""
        action.claim_expires_at = None
    action.lease_owner = ""
    action.lease_expires_at = None
    action.result = {
        **(action.result or {}),
        "generation_stage": "ai_result_persist_unknown",
        "generation_outcome": "ai_result_persist_unknown",
    }
    _release_runtime_resources(action)


def recover_stale_pre_gateway_generation(action: Action) -> bool:
    data = dict(action.payload or {})
    if not _is_generating_ai_action(action, data):
        return False
    attempt_id = str(data.get("ai_generation_attempt_id") or "")
    if (action.result or {}).get("ai_provider_call_started_at"):
        mark_attempt_outcome(
            data,
            attempt_id,
            "ai_result_persist_unknown",
            timestamp=_now(),
        )
        data["ai_generation_status"] = "ai_result_persist_unknown"
        _reset_action_for_recovery(action, data, clear_claim=False)
        action.executed_at = None
        return True
    mark_attempt_outcome(data, attempt_id, "stale_worker_recovered", timestamp=_now())
    data.update({
        "ai_generation_status": "pending",
        "ai_generation_attempt_id": "",
        "ai_generation_request_id": "",
        "ai_generation_claim_owner": "",
        "ai_generation_claim_token": "",
    })
    action.payload = data
    action.status = "pending"
    action.executed_at = None
    action.lease_owner = ""
    action.lease_expires_at = None
    action.result = {
        **(action.result or {}),
        "generation_stage": "generation_recovery",
        "generation_outcome": "retry_pending",
        "recovered_ai_generation_attempt_id": attempt_id,
    }
    _release_runtime_resources(action)
    return True


def _is_generating_ai_action(action: Action, data: dict) -> bool:
    generation_action = (
        (action.task_type == "group_ai_chat" and action.action_type == "send_message")
        or (action.task_type == "channel_comment" and action.action_type == "post_comment")
    )
    return generation_action and data.get("ai_generation_status") == "generating"


__all__ = ["persist_generation_unknown", "recover_stale_pre_gateway_generation"]
