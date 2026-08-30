from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, AuditLog, Task
from app.models.group_clone import (
    CloneDeliveryObligation,
    CloneSequencerHeadCase,
    TelegramGatewayMutationIdentity,
)
from app.services._common import _now


def decide_clone_sequencer_case(
    session: Session,
    task: Task,
    *,
    case_id: str,
    request,
    actor_id: int,
) -> dict:
    case = session.scalar(select(CloneSequencerHeadCase).where(
        CloneSequencerHeadCase.id == case_id,
        CloneSequencerHeadCase.task_id == task.id,
    ).with_for_update())
    if case is None:
        raise LookupError("Case 不存在")
    fingerprint = _request_fingerprint(request)
    replay = _replay(case, request.client_request_id, fingerprint)
    if replay is not None:
        return replay
    _validate_request(case, request)
    obligation = session.get(CloneDeliveryObligation, case.obligation_id)
    if obligation is None:
        raise RuntimeError("group_clone_sequencer_obligation_missing")
    _apply_decision(session, case, obligation=obligation, request=request)
    case.decision_actor_id = actor_id
    case.revision += 1
    result = {"success": True, "case_id": case.id, "state": case.state}
    evidence = dict(case.failure_evidence or {})
    evidence["decision_request"] = {
        "client_request_id": request.client_request_id,
        "fingerprint": fingerprint,
        "result": result,
    }
    case.failure_evidence = evidence
    session.add(_audit(task, case=case, request=request, actor_id=actor_id))
    return result


def _replay(case, client_request_id: str, fingerprint: str) -> dict | None:
    stored = dict((case.failure_evidence or {}).get("decision_request") or {})
    if stored.get("client_request_id") != client_request_id:
        return None
    if stored.get("fingerprint") != fingerprint:
        raise ValueError("client_request_id 已用于不同的 Sequencer 决策")
    result = stored.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("group_clone_sequencer_replay_result_missing")
    return result


def _validate_request(case, request) -> None:
    if case.revision != request.expected_case_revision:
        raise ValueError("Case revision 已变化")
    if request.decision != "retry_same_mutation":
        return
    if case.case_kind == "unknown_deadline_closed":
        raise ValueError("永久 unknown 不允许重试 mutation")
    if case.remote_mutation_started and not case.authoritative_absence_evidence_id:
        raise ValueError("远端 mutation 已开始且无权威未执行证据，禁止重试")


def _apply_decision(session, case, *, obligation, request) -> None:
    case.decision_reason = request.reason
    if request.decision == "accept_visible_gap":
        case.state = "visible_gap_accepted"
        return
    if request.decision == "keep_blocked":
        case.state = "blocked"
        return
    case.state = "retry_authorized"
    _authorize_retry(session, obligation)


def _authorize_retry(session, obligation) -> None:
    action = session.scalar(select(Action).where(
        Action.obligation_id == obligation.id,
        Action.action_type.in_(("group_clone_send", "group_clone_mutation")),
    ).order_by(Action.created_at.desc()).limit(1).with_for_update())
    if action is None:
        raise RuntimeError("group_clone_retry_action_missing")
    identity_id = str((action.payload or {}).get("gateway_mutation_identity_id") or "")
    identity = session.get(TelegramGatewayMutationIdentity, identity_id)
    if identity is None:
        raise RuntimeError("group_clone_retry_identity_missing")
    action.status = "pending"
    action.executed_at = None
    action.scheduled_at = _now()
    action.action_version += 1
    action.result = {**dict(action.result or {}), "retry_authorized": True}
    identity.state = "allocated"
    obligation.state = "action_bound"
    obligation.version += 1


def _request_fingerprint(request) -> str:
    raw = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _audit(task, *, case, request, actor_id):
    return AuditLog(
        tenant_id=task.tenant_id,
        actor=str(actor_id),
        action="clone_sequencer_head_decided",
        target_type="clone_sequencer_head_case",
        target_id=case.id,
        detail=json.dumps({
            "task_id": task.id,
            "decision": request.decision,
            "reason": request.reason,
            "revision": case.revision,
        }, ensure_ascii=False, sort_keys=True),
    )


__all__ = ["decide_clone_sequencer_case"]
