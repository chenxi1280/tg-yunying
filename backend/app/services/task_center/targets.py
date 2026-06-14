from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GroupAuthStatus, OperationTarget, TgGroup


def group_from_reference(
    session: Session,
    tenant_id: int,
    *,
    group_id: int | None = None,
    operation_target_id: int | None = None,
    require_authorized: bool = False,
) -> TgGroup | None:
    target = session.get(OperationTarget, int(operation_target_id)) if operation_target_id else None
    if target:
        if target.tenant_id != tenant_id or target.target_type != "group":
            return None
        group = session.scalar(
            select(TgGroup)
            .where(
                TgGroup.tenant_id == tenant_id,
                TgGroup.tg_peer_id == target.tg_peer_id,
            )
            .order_by(TgGroup.id.asc())
            .limit(1)
        )
        if group and (not require_authorized or group.auth_status == GroupAuthStatus.AUTHORIZED.value):
            return group
        return _group_from_target_title(session, tenant_id, target, require_authorized=require_authorized)
    group = session.get(TgGroup, int(group_id)) if group_id else None
    if group and group.tenant_id == tenant_id and (not require_authorized or group.auth_status == GroupAuthStatus.AUTHORIZED.value):
        return group
    return None


def _group_from_target_title(session: Session, tenant_id: int, target: OperationTarget, *, require_authorized: bool) -> TgGroup | None:
    if not target.title:
        return None
    groups = list(
        session.scalars(
            select(TgGroup)
            .where(TgGroup.tenant_id == tenant_id, TgGroup.title == target.title)
            .order_by(TgGroup.id.asc())
        )
    )
    for group in sorted(groups, key=_group_target_fallback_rank):
        if require_authorized and group.auth_status != GroupAuthStatus.AUTHORIZED.value:
            continue
        return group
    return None


def _group_target_fallback_rank(group: TgGroup) -> tuple[int, int, int]:
    stable_rank = 0 if str(group.tg_peer_id or "").lstrip("-").isdigit() else 1
    send_rank = 0 if group.can_send else 1
    return (stable_rank, send_rank, int(group.id or 0))


def group_ids_from_operation_targets(session: Session, tenant_id: int, operation_target_ids: list[int]) -> list[int]:
    ids: list[int] = []
    for target_id in operation_target_ids:
        group = group_from_reference(session, tenant_id, operation_target_id=target_id, require_authorized=True)
        if group and group.id not in ids:
            ids.append(group.id)
    return ids


__all__ = ["group_from_reference", "group_ids_from_operation_targets"]
