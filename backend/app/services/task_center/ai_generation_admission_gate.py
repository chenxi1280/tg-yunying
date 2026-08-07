from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.models import Action, Task
from app.services._common import _now

from .ai_generator import AiGenerationUnavailable
from .payloads import SendMessagePayload


ADMISSION_RETRY_SECONDS = 30


def defer_generation_for_group_bot_admission(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
) -> bool:
    task = session.get(Task, action.task_id)
    if _fact_first_admission_required(task):
        return _defer_fact_first_admission(session, action, payload)
    return _defer_legacy_admission(session, action, payload)


def _fact_first_admission_required(task: Task | None) -> bool:
    if task is None or task.fulfillment_contract_version != "fact_first_v3":
        return False
    config = task.type_config if isinstance(task.type_config, dict) else {}
    return bool(config.get("group_bot_admission_required"))


def _defer_fact_first_admission(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
) -> bool:
    from .task_group_bot_admission_v2 import evaluate_task_admission

    decision = evaluate_task_admission(
        session,
        task_id=action.task_id,
        tenant_id=action.tenant_id,
        group_id=int(payload.group_id or 0),
        account_id=int(action.account_id or 0),
    )
    if decision.allowed:
        _record_allowed_fact_first(action, decision)
        session.commit()
        return False
    if decision.code == "c2_account_abandoned":
        _persist_fact_first_abandonment(session, action, decision)
        raise AiGenerationUnavailable(decision.code)
    _record_waiting_fact_first(action, decision)
    session.commit()
    return True


def _record_waiting_fact_first(action: Action, decision) -> None:
    action.result = {
        **dict(action.result or {}),
        "error_code": decision.code,
        "validation_stage": "pre_ai_task_group_bot_admission",
        "task_group_bot_admission_id": decision.admission_id,
        "task_group_bot_admission_version": decision.version,
    }
    action.scheduled_at = _now() + timedelta(seconds=ADMISSION_RETRY_SECONDS)


def _record_allowed_fact_first(action: Action, decision) -> None:
    action.payload = {
        **dict(action.payload or {}),
        "task_group_bot_admission_id": decision.admission_id,
        "task_group_bot_admission_version": decision.version,
    }
    result = dict(action.result or {})
    if result.get("validation_stage") != "pre_ai_task_group_bot_admission":
        return
    result.pop("error_code", None)
    result.pop("validation_stage", None)
    action.result = result


def _persist_fact_first_abandonment(
    session: Session,
    action: Action,
    decision,
) -> None:
    from . import dispatcher

    dispatcher._abandon_fact_first_account_for_task(
        session,
        action,
        reason=decision.terminal_reason,
    )
    dispatcher._skip(
        action,
        decision.code,
        "该账号在当前任务与目标群组合内已放弃，不再重试",
    )
    session.commit()


def _defer_legacy_admission(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
) -> bool:
    if not payload.group_bot_admission_state:
        return False
    from .group_bot_admission import evaluate_send_gate

    decision = evaluate_send_gate(
        session,
        tenant_id=action.tenant_id,
        group_id=int(payload.group_id or 0),
        account_id=int(action.account_id or 0),
        enforce=True,
        action_id=str(action.id),
    )
    if decision.allowed:
        _record_allowed_legacy(action, decision)
        session.commit()
        return False
    action.result = {
        **dict(action.result or {}),
        "error_code": decision.code or "group_bot_admission_wait",
        "validation_stage": "pre_ai_group_bot_admission",
        "group_bot_admission_state": decision.state,
        "group_bot_admission_id": decision.admission_id,
    }
    action.scheduled_at = _now() + timedelta(seconds=ADMISSION_RETRY_SECONDS)
    session.commit()
    return True


def _record_allowed_legacy(action: Action, decision) -> None:
    payload = dict(action.payload or {})
    payload.update({
        "group_bot_admission_id": decision.admission_id,
        "group_bot_admission_state": decision.state,
        "admission_version": decision.admission_version,
    })
    if decision.code == "post_follow_visibility_probe":
        payload["group_bot_post_follow_visibility_probe"] = True
    action.payload = payload
    result = dict(action.result or {})
    if result.get("validation_stage") != "pre_ai_group_bot_admission":
        return
    for key in (
        "error_code",
        "validation_stage",
        "group_bot_admission_state",
        "group_bot_admission_id",
    ):
        result.pop(key, None)
    action.result = result


__all__ = ["defer_generation_for_group_bot_admission"]
