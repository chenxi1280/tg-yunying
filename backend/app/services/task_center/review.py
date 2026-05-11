from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ReviewQueue
from app.services._common import _now


def queue_review(session: Session, action: Action, *, content: str, source_info: str = "", ttl_hours: int = 24) -> ReviewQueue:
    review = ReviewQueue(
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        action_id=action.id,
        content_preview=content[:4000],
        source_info=source_info[:500],
        status="pending",
        expires_at=_now() + timedelta(hours=ttl_hours),
    )
    session.add(review)
    return review


def expire_reviews(session: Session) -> int:
    rows = list(
        session.scalars(
            select(ReviewQueue).where(
                ReviewQueue.status == "pending",
                ReviewQueue.expires_at.is_not(None),
                ReviewQueue.expires_at <= _now(),
            )
        )
    )
    for review in rows:
        review.status = "expired"
        action = session.get(Action, review.action_id)
        if action and action.status == "pending":
            action.status = "skipped"
            action.result = {"success": False, "error_code": "review_expired", "error_message": "内容处理已过期"}
            action.executed_at = _now()
    return len(rows)


def has_pending_review(session: Session, action_id: str) -> bool:
    return bool(session.scalar(select(ReviewQueue.id).where(ReviewQueue.action_id == action_id, ReviewQueue.status == "pending").limit(1)))


__all__ = ["expire_reviews", "has_pending_review", "queue_review"]
