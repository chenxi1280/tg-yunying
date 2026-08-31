from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import CurrentUser, ensure_permission, get_current_user
from app.database import get_session
from app.models import AuditLog
from app.models.group_clone import (
    CloneDeliveryObligation,
    CloneManualReviewDecision,
)
from app.schemas.task_center import (
    GroupCloneManualReviewDecisionRequest,
    GroupCloneSenderBindingChangeRequest,
)
from app.services._common import _now
from app.services.task_center.group_clone_lifecycle import tenant_clone_task
from app.services.task_center.group_clone_manual_review import (
    allowed_manual_review_decisions,
)
from app.services.task_center.group_clone_binding import CloneSenderBindingManager

router = APIRouter(prefix="/api/tasks", tags=["group_clone"])


@router.post("/{task_id}/clone-bindings/{binding_id}/change")
def change_clone_sender_binding(
    task_id: str,
    binding_id: str,
    *,
    req: GroupCloneSenderBindingChangeRequest,
    db: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    ensure_permission(current_user, "tasks.manage")
    task = _require_task(db, current_user, task_id)
    try:
        replay = _binding_request_replay(db, task_id, binding_id, req)
        if replay is not None:
            return replay
        replacement = CloneSenderBindingManager.release_or_rebind(
            db, task, binding_id=binding_id,
            expected_binding_version=req.expected_binding_version,
            replacement_account_id=req.replacement_account_id,
            reason=req.reason,
        )
        result = {
            "success": True,
            "released_binding_id": binding_id,
            "replacement_binding_id": replacement.id if replacement else None,
        }
        db.add(_binding_audit(current_user, task_id, binding_id, req, result))
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _binding_audit(current_user, task_id, binding_id, request, result):
    return AuditLog(
        tenant_id=current_user.tenant_id,
        actor=str(current_user.id),
        action="clone_binding_reassigned" if result["replacement_binding_id"] else "clone_binding_released",
        target_type="clone_sender_binding",
        target_id=binding_id,
        detail=json.dumps({
            "task_id": task_id,
            "replacement_account_id": request.replacement_account_id,
            "expected_binding_version": request.expected_binding_version,
            "reason": request.reason,
            "client_request_id": request.client_request_id,
            "request_fingerprint": _binding_request_fingerprint(request),
            "result": result,
        }, ensure_ascii=False, sort_keys=True),
    )


def _binding_request_replay(session, task_id, binding_id, request):
    rows = session.scalars(select(AuditLog).where(
        AuditLog.target_type == "clone_sender_binding",
        AuditLog.target_id == binding_id,
        AuditLog.action.in_(("clone_binding_reassigned", "clone_binding_released")),
    ).order_by(AuditLog.created_at.desc())).all()
    for row in rows:
        detail = json.loads(row.detail or "{}")
        if detail.get("task_id") != task_id:
            continue
        if detail.get("client_request_id") != request.client_request_id:
            continue
        if detail.get("request_fingerprint") != _binding_request_fingerprint(request):
            raise ValueError("client_request_id 已用于不同的 sender binding 请求")
        return dict(detail.get("result") or {})
    return None


def _binding_request_fingerprint(request) -> str:
    raw = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


@router.post("/{task_id}/clone-manual-reviews/{review_id}/decision")
def decide_clone_manual_review(
    task_id: str,
    review_id: str,
    *,
    req: GroupCloneManualReviewDecisionRequest,
    db: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    ensure_permission(current_user, "tasks.manage")
    task = _require_task(db, current_user, task_id)
    obligation = db.scalar(select(CloneDeliveryObligation).where(
        CloneDeliveryObligation.id == review_id,
        CloneDeliveryObligation.task_id == task_id,
        CloneDeliveryObligation.epoch == task.task_lifecycle_epoch,
    ).with_for_update())
    if obligation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="人工审核项不存在")
    replay = _request_replay(db, obligation.id, req)
    if replay:
        return _decision_result(replay)
    revision = _next_review_revision(db, obligation.id)
    if revision != req.expected_review_revision:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="人工审核 revision 已变化")
    if obligation.state != "waiting_manual_review":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="人工审核项已不在待处理状态")
    allowed = allowed_manual_review_decisions(obligation.error_code)
    if req.decision not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"当前原因不允许 {req.decision}，可选决定: {list(allowed)}",
        )
    before = _fingerprint(obligation)
    _apply_decision(obligation, req.decision)
    decision = _new_review_decision(
        obligation, req, current_user, revision=revision, before=before,
    )
    db.add(decision)
    db.flush()
    db.add(_audit(
        current_user, task_id, obligation=obligation, decision=decision,
    ))
    db.commit()
    return _decision_result(decision)


def _new_review_decision(obligation, request, current_user, *, revision, before):
    return CloneManualReviewDecision(
        obligation_id=obligation.id,
        review_revision=revision,
        client_request_id=request.client_request_id,
        decision=request.decision,
        actor_id=current_user.id,
        actor_name=str(current_user.id),
        reason=request.reason,
        before_fingerprint=before,
        after_fingerprint=_fingerprint(obligation),
    )


def _apply_decision(obligation, decision) -> None:
    if decision == "release":
        obligation.state = "observed"
        obligation.error_code = None
        obligation.resolved_at = None
        obligation.version += 1
        return
    if decision == "drop":
        obligation.state = "filtered"
        obligation.resolved_at = _now()
        obligation.version += 1
        return
    obligation.version += 1


def _request_replay(session, obligation_id, request):
    decision = session.scalar(select(CloneManualReviewDecision).where(
        CloneManualReviewDecision.obligation_id == obligation_id,
        CloneManualReviewDecision.client_request_id == request.client_request_id,
    ))
    if decision is None:
        return None
    if (
        decision.review_revision != request.expected_review_revision
        or decision.decision != request.decision
        or decision.reason != request.reason
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="client_request_id 已用于不同的人工审核请求",
        )
    return decision


def _next_review_revision(session, obligation_id) -> int:
    latest = session.scalar(select(CloneManualReviewDecision.review_revision).where(
        CloneManualReviewDecision.obligation_id == obligation_id,
    ).order_by(CloneManualReviewDecision.review_revision.desc()).limit(1))
    return int(latest or 0) + 1


def _decision_result(decision):
    return {
        "success": True,
        "review_id": decision.obligation_id,
        "review_revision": decision.review_revision,
        "decision": decision.decision,
        "obligation_state": {
            "release": "observed",
            "drop": "filtered",
            "keep_blocked": "waiting_manual_review",
        }[decision.decision],
    }


def _fingerprint(obligation) -> str:
    raw = json.dumps({
        "id": obligation.id,
        "state": obligation.state,
        "error_code": obligation.error_code,
        "version": obligation.version,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _audit(current_user, task_id, *, obligation, decision):
    return AuditLog(
        tenant_id=current_user.tenant_id,
        actor=str(current_user.id),
        action="clone_manual_decided",
        target_type="clone_delivery_obligation",
        target_id=obligation.id,
        detail=json.dumps({
            "task_id": task_id,
            "review_revision": decision.review_revision,
            "decision": decision.decision,
            "reason": decision.reason,
        }, ensure_ascii=False, sort_keys=True),
    )


def _require_task(session, current_user, task_id):
    try:
        return tenant_clone_task(session, current_user.tenant_id or 1, task_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


__all__ = ["router"]
