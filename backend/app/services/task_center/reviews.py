from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ReviewQueue
from app.schemas.task_center import ReviewApproveRequest, ReviewRejectRequest
from app.services._common import _now, audit

from .review import expire_reviews


class ReviewStateError(ValueError):
    """Raised when an operator tries to transition a terminal review."""


def list_reviews(session: Session, tenant_id: int, status: str | None = None, task_id: str | None = None) -> list[ReviewQueue]:
    if expire_reviews(session):
        session.commit()
    stmt = select(ReviewQueue).where(ReviewQueue.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(ReviewQueue.status == status)
    if task_id:
        stmt = stmt.where(ReviewQueue.task_id == task_id)
    return list(session.scalars(stmt.order_by(ReviewQueue.created_at.desc()).limit(500)))


def approve_review(session: Session, tenant_id: int, review_id: str, payload: ReviewApproveRequest, actor: str) -> ReviewQueue:
    review = _get_review(session, tenant_id, review_id)
    if review.status != "pending":
        raise ReviewStateError("只能处理待处理内容")
    action = session.get(Action, review.action_id)
    if not action:
        raise ValueError("action not found")
    data = dict(action.payload or {})
    if payload.edited_content:
        if action.action_type == "post_comment":
            data["comment_text"] = payload.edited_content
        else:
            data["message_text"] = payload.edited_content
        review.content_preview = payload.edited_content[:4000]
    data["review_approved"] = True
    action.payload = data
    review.status = "approved"
    review.reviewed_by = actor
    review.reviewed_at = _now()
    action.status = "pending"
    action.scheduled_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="处理通过任务动作", target_type="review_queue", target_id=review.id)
    session.commit()
    session.refresh(review)
    return review


def reject_review(session: Session, tenant_id: int, review_id: str, payload: ReviewRejectRequest, actor: str) -> ReviewQueue:
    review = _get_review(session, tenant_id, review_id)
    if review.status != "pending":
        raise ReviewStateError("只能跳过待处理内容")
    action = session.get(Action, review.action_id)
    review.status = "rejected"
    review.reviewed_by = actor
    review.reviewed_at = _now()
    review.reject_reason = payload.reason
    if action:
        action.status = "skipped"
        action.executed_at = _now()
        action.result = {"success": False, "error_code": "review_rejected", "error_message": payload.reason or "内容处理跳过"}
    audit(session, tenant_id=tenant_id, actor=actor, action="处理跳过任务动作", target_type="review_queue", target_id=review.id)
    session.commit()
    session.refresh(review)
    return review


def _get_review(session: Session, tenant_id: int, review_id: str) -> ReviewQueue:
    review = session.get(ReviewQueue, review_id)
    if not review or review.tenant_id != tenant_id:
        raise ValueError("review not found")
    return review


__all__ = ["ReviewStateError", "approve_review", "list_reviews", "reject_review"]
