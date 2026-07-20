from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OperationTarget, TgGroup


TARGET_REFERENCE_OPERATION_TARGET = "operation_target"
TARGET_REFERENCE_TG_GROUP = "tg_group"


def rank_deboost_target_group_refs(
    session: Session,
    tenant_id: int,
    target_group_ids: list[int],
    *,
    reference_type: str | None = None,
) -> list[dict[str, Any]]:
    target_ids = _target_ids(target_group_ids)
    if reference_type == TARGET_REFERENCE_OPERATION_TARGET:
        return _ordered_refs(target_ids, _operation_target_refs(session, tenant_id, target_ids))
    if reference_type == TARGET_REFERENCE_TG_GROUP:
        return _ordered_refs(target_ids, _tg_group_refs(session, tenant_id, target_ids))
    if reference_type:
        raise ValueError(f"搜索排名观察目标群引用类型无效：{reference_type}")
    return _legacy_target_group_refs(session, tenant_id, target_ids)


def _legacy_target_group_refs(session: Session, tenant_id: int, target_ids: list[int]) -> list[dict[str, Any]]:
    operation_refs = _operation_target_refs(session, tenant_id, target_ids)
    group_refs = _tg_group_refs(session, tenant_id, target_ids)
    refs_by_id: dict[int, dict[str, Any]] = {}
    for target_id in target_ids:
        operation_ref = operation_refs.get(target_id)
        group_ref = group_refs.get(target_id)
        if operation_ref and group_ref and not _same_telegram_identity(operation_ref, group_ref):
            raise ValueError(
                f"搜索排名观察目标群 ID {target_id} 存在引用类型歧义，请显式设置 target_reference_type"
            )
        ref = operation_ref or group_ref
        if ref is not None:
            refs_by_id[target_id] = ref
    return _ordered_refs(target_ids, refs_by_id)


def _operation_target_refs(session: Session, tenant_id: int, target_ids: list[int]) -> dict[int, dict[str, Any]]:
    targets = session.scalars(select(OperationTarget).where(
        OperationTarget.tenant_id == tenant_id,
        OperationTarget.id.in_(target_ids),
        OperationTarget.target_type == "group",
    ))
    return {
        target.id: _target_group_ref(target.id, target.tg_peer_id, target.username)
        for target in targets
    }


def _tg_group_refs(session: Session, tenant_id: int, target_ids: list[int]) -> dict[int, dict[str, Any]]:
    groups = list(session.scalars(select(TgGroup).where(TgGroup.tenant_id == tenant_id, TgGroup.id.in_(target_ids))))
    targets_by_peer = _targets_by_peer(session, tenant_id, groups)
    return {
        group.id: _target_group_ref(
            group.id,
            group.tg_peer_id,
            _target_username(targets_by_peer, group.tg_peer_id),
        )
        for group in groups
    }


def require_rank_deboost_target_group_refs(
    session: Session,
    tenant_id: int,
    target_group_ids: list[int],
    *,
    reference_type: str | None = None,
) -> list[dict[str, Any]]:
    target_ids = _target_ids(target_group_ids)
    refs = rank_deboost_target_group_refs(
        session,
        tenant_id,
        target_ids,
        reference_type=reference_type,
    )
    refs_by_id = {int(item["group_id"]): item for item in refs}
    unresolved = [
        group_id
        for group_id in target_ids
        if not _has_target_identity(refs_by_id.get(group_id))
    ]
    if unresolved:
        raise ValueError(f"搜索排名观察目标群缺少可验证 username：{unresolved}")
    return refs


def _targets_by_peer(session: Session, tenant_id: int, groups: list[TgGroup]) -> dict[str, OperationTarget]:
    group_peer_ids = [group.tg_peer_id for group in groups]
    return {
        target.tg_peer_id: target
        for target in session.scalars(select(OperationTarget).where(
            OperationTarget.tenant_id == tenant_id,
            OperationTarget.target_type == "group",
            OperationTarget.tg_peer_id.in_(group_peer_ids),
        ))
    }


def _ordered_refs(target_ids: list[int], refs_by_id: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return [refs_by_id[target_id] for target_id in target_ids if target_id in refs_by_id]


def _same_telegram_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_peer_id = str(left.get("peer_id") or "").strip()
    right_peer_id = str(right.get("peer_id") or "").strip()
    return bool(left_peer_id) and left_peer_id == right_peer_id


def _target_ids(values: list[int]) -> list[int]:
    target_ids: list[int] = []
    for value in values:
        try:
            target_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("搜索排名观察目标群 ID 无效") from exc
        if target_id <= 0:
            raise ValueError("搜索排名观察目标群 ID 无效")
        if target_id not in target_ids:
            target_ids.append(target_id)
    if not target_ids:
        raise ValueError("搜索排名观察任务缺少我方目标群 ID")
    return target_ids


def _has_target_identity(ref: dict[str, Any] | None) -> bool:
    if ref is None:
        return False
    return bool(str(ref.get("username") or "").strip())


def _target_group_ref(group_id: int, peer_id: str, username: str) -> dict[str, Any]:
    return {
        "group_id": group_id,
        "peer_id": str(peer_id or ""),
        "username": str(username or "").strip().lstrip("@"),
    }


def _target_username(targets_by_peer: dict[str, OperationTarget], peer_id: str) -> str:
    target = targets_by_peer.get(peer_id)
    return target.username if target is not None else ""


__all__ = [
    "TARGET_REFERENCE_OPERATION_TARGET",
    "TARGET_REFERENCE_TG_GROUP",
    "rank_deboost_target_group_refs",
    "require_rank_deboost_target_group_refs",
]
