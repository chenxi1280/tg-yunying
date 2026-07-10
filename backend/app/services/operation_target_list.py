from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from app.models import GroupAuthStatus, OperationTarget, TgAccount, TgGroup, TgGroupAccount


DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_QUERY_LENGTH = 120
MAX_SELECTED_IDS = 100
VALID_CAPABILITIES = frozenset({"send", "listen", "archive", "task"})

__all__ = ["OperationTargetListQuery", "list_operation_targets_page"]


@dataclass(frozen=True)
class OperationTargetListQuery:
    tenant_id: int
    page: int | None = None
    page_size: int | None = None
    target_type: str | None = None
    account_id: int | None = None
    q: str = ""
    ids: tuple[int, ...] = ()
    linked_group_id: int | None = None
    capability: str | None = None


@dataclass(frozen=True)
class _LinkSummary:
    all_count: int = 0
    send_count: int = 0
    listener_count: int = 0


def list_operation_targets_page(
    session: Session,
    query: OperationTargetListQuery,
) -> tuple[list[dict[str, Any]], int]:
    normalized = _normalize_query(query)
    _validate_account_scope(session, normalized)
    base = _operation_target_rows(normalized)
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = session.execute(_paged_target_rows(base, normalized)).mappings().all()
    group_ids = tuple({row["linked_group_id"] for row in rows if row["linked_group_id"] is not None})
    summaries = _link_summaries(session, normalized.tenant_id, group_ids)
    return [_operation_target_row_payload(row, summaries) for row in rows], int(total)


def _normalize_query(query: OperationTargetListQuery) -> OperationTargetListQuery:
    normalized_q = query.q.strip()
    if len(normalized_q) > MAX_QUERY_LENGTH:
        raise ValueError(f"q must be at most {MAX_QUERY_LENGTH} characters")
    normalized_ids = tuple(dict.fromkeys(query.ids))
    if len(normalized_ids) > MAX_SELECTED_IDS:
        raise ValueError(f"ids must contain at most {MAX_SELECTED_IDS} values")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in normalized_ids):
        raise ValueError("ids must contain positive integers")
    if query.capability is not None and query.capability not in VALID_CAPABILITIES:
        raise ValueError("invalid capability")
    return replace(
        query,
        q=normalized_q,
        ids=normalized_ids,
        **_normalized_paging(query),
    )


def _normalized_paging(query: OperationTargetListQuery) -> dict[str, int | None]:
    if query.page is None and query.page_size is None:
        return {"page": None, "page_size": None}
    page = query.page or DEFAULT_PAGE
    page_size = query.page_size or DEFAULT_PAGE_SIZE
    if page < 1:
        raise ValueError("page must be positive")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
    return {"page": page, "page_size": page_size}


def _validate_account_scope(session: Session, query: OperationTargetListQuery) -> None:
    if query.account_id is None:
        return
    account_id = session.scalar(
        select(TgAccount.id).where(
            TgAccount.id == query.account_id,
            TgAccount.tenant_id == query.tenant_id,
            TgAccount.deleted_at.is_(None),
        )
    )
    if account_id is None:
        raise ValueError("account not found")


def _operation_target_rows(query: OperationTargetListQuery):
    return (
        select(
            OperationTarget.id,
            OperationTarget.tenant_id,
            OperationTarget.target_type,
            OperationTarget.tg_peer_id,
            OperationTarget.title,
            OperationTarget.username,
            OperationTarget.member_count,
            OperationTarget.can_send,
            OperationTarget.auth_status,
            OperationTarget.last_sync_at,
            OperationTarget.created_at,
            OperationTarget.updated_at,
            TgGroup.id.label("linked_group_id"),
            TgGroup.listener_enabled.label("group_listener_enabled"),
        )
        .select_from(OperationTarget)
        .outerjoin(TgGroup, _linked_group_condition())
        .where(*_target_filters(query))
    )


def _linked_group_condition():
    return and_(
        TgGroup.tenant_id == OperationTarget.tenant_id,
        TgGroup.tg_peer_id == OperationTarget.tg_peer_id,
    )


def _target_filters(query: OperationTargetListQuery) -> list[Any]:
    filters: list[Any] = [OperationTarget.tenant_id == query.tenant_id]
    if query.target_type:
        filters.append(OperationTarget.target_type == query.target_type)
    if query.account_id is not None:
        filters.append(_account_link_exists(query))
    if query.q:
        filters.append(_search_filter(query.q))
    if query.ids:
        filters.append(OperationTarget.id.in_(query.ids))
    if query.linked_group_id is not None:
        filters.append(TgGroup.id == query.linked_group_id)
    if query.capability is not None:
        filters.append(_capability_filter(query))
    return filters


def _account_link_exists(query: OperationTargetListQuery):
    return exists(
        select(1).where(
            TgGroupAccount.tenant_id == query.tenant_id,
            TgGroupAccount.group_id == TgGroup.id,
            TgGroupAccount.account_id == query.account_id,
            TgGroupAccount.can_send.is_(True),
        )
    )


def _search_filter(value: str):
    pattern = f"%{value}%"
    filters = [
        OperationTarget.title.ilike(pattern),
        OperationTarget.username.ilike(pattern),
        OperationTarget.tg_peer_id.ilike(pattern),
        OperationTarget.auth_status.ilike(pattern),
    ]
    if value.isdigit():
        filters.append(OperationTarget.id == int(value))
    return or_(*filters)


def _capability_filter(query: OperationTargetListQuery):
    if query.capability == "send":
        return OperationTarget.can_send.is_(True)
    if query.capability == "listen":
        return and_(
            TgGroup.id.is_not(None),
            or_(TgGroup.listener_enabled.is_(True), _listener_link_exists(query.tenant_id)),
        )
    if query.capability == "archive":
        return and_(
            TgGroup.id.is_not(None),
            OperationTarget.target_type == "group",
            OperationTarget.auth_status == GroupAuthStatus.AUTHORIZED.value,
        )
    return _task_capability_filter()


def _listener_link_exists(tenant_id: int):
    return exists(
        select(1).where(
            TgGroupAccount.tenant_id == tenant_id,
            TgGroupAccount.group_id == TgGroup.id,
            TgGroupAccount.is_listener.is_(True),
        )
    )


def _task_capability_filter():
    channel_capability = and_(
        OperationTarget.target_type == "channel",
        OperationTarget.auth_status.in_(
            (GroupAuthStatus.UNVERIFIED.value, GroupAuthStatus.AUTHORIZED.value)
        ),
    )
    group_capability = and_(
        OperationTarget.target_type == "group",
        OperationTarget.auth_status == GroupAuthStatus.AUTHORIZED.value,
    )
    return or_(channel_capability, group_capability)


def _paged_target_rows(statement, query: OperationTargetListQuery):
    ordered = statement.order_by(OperationTarget.id.desc())
    if query.page is None or query.page_size is None:
        return ordered
    return ordered.offset((query.page - 1) * query.page_size).limit(query.page_size)


def _link_summaries(
    session: Session,
    tenant_id: int,
    group_ids: tuple[int, ...],
) -> dict[int, _LinkSummary]:
    if not group_ids:
        return {}
    statement = (
        select(
            TgGroupAccount.group_id,
            func.count(TgGroupAccount.id).label("all_count"),
            func.count(TgGroupAccount.id).filter(TgGroupAccount.can_send.is_(True)).label("send_count"),
            func.count(TgGroupAccount.id).filter(TgGroupAccount.is_listener.is_(True)).label("listener_count"),
        )
        .where(
            TgGroupAccount.tenant_id == tenant_id,
            TgGroupAccount.group_id.in_(group_ids),
        )
        .group_by(TgGroupAccount.group_id)
    )
    return {
        row["group_id"]: _LinkSummary(
            all_count=int(row["all_count"]),
            send_count=int(row["send_count"]),
            listener_count=int(row["listener_count"]),
        )
        for row in session.execute(statement).mappings()
    }


def _operation_target_row_payload(
    row: Mapping[str, Any],
    summaries: Mapping[int, _LinkSummary],
) -> dict[str, Any]:
    summary = summaries.get(row["linked_group_id"], _LinkSummary())
    task_capabilities = _task_capabilities(row, summary)
    linked = row["linked_group_id"] is not None
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "target_type": row["target_type"],
        "tg_peer_id": row["tg_peer_id"],
        "title": row["title"],
        "username": row["username"],
        "member_count": row["member_count"],
        "can_send": row["can_send"],
        "auth_status": row["auth_status"],
        "linked_group_id": row["linked_group_id"],
        "can_listen": linked and bool(row["group_listener_enabled"] or summary.listener_count),
        "can_archive": linked and _can_archive(row),
        "can_task": bool(task_capabilities),
        "task_capabilities": task_capabilities,
        "available_send_account_count": _available_send_count(row, summary),
        "listener_account_count": summary.listener_count,
        "last_sync_at": row["last_sync_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _can_archive(row: Mapping[str, Any]) -> bool:
    return (
        row["target_type"] == "group"
        and row["auth_status"] == GroupAuthStatus.AUTHORIZED.value
    )


def _available_send_count(row: Mapping[str, Any], summary: _LinkSummary) -> int:
    if row["target_type"] == "channel":
        return summary.all_count
    return summary.send_count


def _task_capabilities(row: Mapping[str, Any], summary: _LinkSummary) -> list[str]:
    if row["target_type"] == "channel":
        if row["auth_status"] in {GroupAuthStatus.UNVERIFIED.value, GroupAuthStatus.AUTHORIZED.value}:
            return ["频道浏览", "频道点赞", "频道评论/回复"]
        return []
    if row["auth_status"] != GroupAuthStatus.AUTHORIZED.value:
        return []
    capabilities: list[str] = []
    if row["linked_group_id"] is not None and row["can_send"] and summary.send_count:
        capabilities.append("AI 活跃群")
    if row["linked_group_id"] is not None and summary.listener_count:
        capabilities.append("转发监听源群")
    if row["linked_group_id"] is not None and row["can_send"] and summary.send_count:
        capabilities.append("转发目标群")
    capabilities.append("群归档")
    return capabilities
