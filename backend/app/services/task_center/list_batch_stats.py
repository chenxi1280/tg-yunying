from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.models import TgAccountSecurityBatch, TgAccountSecurityBatchItem

from .list_candidates import ProfileBatchIndexProjection


def profile_batch_stats(session: Session, batch_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not batch_ids:
        return {}
    item = TgAccountSecurityBatchItem
    latest_item = aliased(TgAccountSecurityBatchItem)
    latest_failure = (
        select(latest_item.failure_type)
        .where(latest_item.batch_id == TgAccountSecurityBatch.id, latest_item.failure_type != "")
        .order_by(latest_item.id.desc())
        .limit(1)
        .correlate(TgAccountSecurityBatch)
        .scalar_subquery()
    )
    statement = _profile_batch_stats_statement(item, latest_failure, batch_ids)
    return {int(row.batch_id): dict(row._mapping) for row in session.execute(statement)}


def _profile_batch_stats_statement(item, latest_failure, batch_ids: list[int]):
    return (
        select(
            TgAccountSecurityBatch.id.label("batch_id"),
            func.count(item.id).label("total_actions"),
            func.count(item.id).filter(item.status == "succeeded").label("success_count"),
            func.count(item.id).filter(item.status.in_(["failed", "partial_success"])).label("failure_count"),
            func.count(item.id).filter(item.status.in_(["skipped", "manual_required"])).label("skipped_count"),
            func.count(item.id).filter(item.status == "manual_required").label("manual_required_count"),
            func.count(item.id).filter(item.status == "pending").label("pending_count"),
            func.count(item.id).filter(item.avatar_status == "waiting_cache").label("waiting_cache_count"),
            func.count(item.id).filter(item.status == "running").label("running_count"),
            latest_failure.label("latest_failure_type"),
        )
        .outerjoin(item, item.batch_id == TgAccountSecurityBatch.id)
        .where(TgAccountSecurityBatch.id.in_(batch_ids))
        .group_by(TgAccountSecurityBatch.id)
    )


def profile_batch_stats_payload(
    batch: TgAccountSecurityBatch | ProfileBatchIndexProjection,
    aggregate: dict[str, Any],
) -> dict[str, Any]:
    total_actions = int(aggregate.get("total_actions") or 0)
    return {
        "total_actions": total_actions or batch.total_count,
        "success_count": int(aggregate.get("success_count") or 0),
        "failure_count": int(aggregate.get("failure_count") or 0),
        "skipped_count": int(aggregate.get("skipped_count") or 0),
        "manual_required_count": int(aggregate.get("manual_required_count") or 0),
        "pending_count": int(aggregate.get("pending_count") or 0),
        "waiting_cache_count": int(aggregate.get("waiting_cache_count") or 0),
        "running_count": int(aggregate.get("running_count") or 0),
        "batch_status": batch.status,
        "latest_failure_type": str(aggregate.get("latest_failure_type") or ""),
    }


__all__ = ["profile_batch_stats", "profile_batch_stats_payload"]
