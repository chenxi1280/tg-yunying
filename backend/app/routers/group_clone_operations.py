from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import CurrentUser, ensure_permission, get_current_user
from app.database import get_session
from app.models import AuditLog
from app.models.group_clone import CloneDeliveryObligation, CloneManualReviewDecision
from app.schemas.task_center import GroupCloneManualReviewDecisionRequest
from app.services._common import _now
from app.services.task_center.group_clone_lifecycle import tenant_clone_task

router = APIRouter(prefix="/api/tasks", tags=["group_clone"])


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
    _require_task(db, current_user, task_id)
    obligation = db.scalar(select(CloneDeliveryObligation).where(
        CloneDeliveryObligation.id == review_id,
        CloneDeliveryObligation.task_id == task_id,
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
    before = _fingerprint(obligation)
    _apply_decision(obligation, req.decision)
    decision = CloneManualReviewDecision(
        obligation_id=obligation.id,
        review_revision=revision,
        client_request_id=req.client_request_id,
        decision=req.decision,
        actor_id=current_user.id,
        actor_name=str(current_user.id),
        reason=req.reason,
        before_fingerprint=before,
        after_fingerprint=_fingerprint(obligation),
    )
    db.add(decision)
    db.flush()
    db.add(_audit(
        current_user, task_id, obligation=obligation, decision=decision,
    ))
    db.commit()
    return _decision_result(decision)


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
