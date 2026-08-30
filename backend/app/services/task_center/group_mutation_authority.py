from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Tuple

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.telegram_authorities import (
    TelegramGroupMutationAuthority,
    TelegramGroupMutationAuthorityHolder,
)

logger = logging.getLogger(__name__)


def compute_route_hash(source_peer_type: str, source_peer_id: str, *, target_peer_type: str, target_peer_id: str) -> str:
    payload = f"{source_peer_type}:{source_peer_id}->{target_peer_type}:{target_peer_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def check_and_claim_exclusive_authority(
    session: Session,
    tenant_id: int,
    *,
    target_peer_type: str, target_peer_id: str,
    writer_kind: str, writer_id: str,
    route_hash: str,
) -> Tuple[bool, str, TelegramGroupMutationAuthority | None]:
    """
    申请目标群的独占写权限（exclusive_clone）。
    若权威已存在且为 shared 且有其他活动写入者，或已由其他 clone 独占，则拒绝。
    """
    _lock_authority_scope(session, tenant_id, target_peer_type=target_peer_type, target_peer_id=target_peer_id)
    auth = _locked_authority(session, tenant_id, target_peer_type=target_peer_type, target_peer_id=target_peer_id)
    if auth is None:
        return _claim_new(session, tenant_id, target_peer_type=target_peer_type, target_peer_id=target_peer_id, writer_kind=writer_kind, writer_id=writer_id, route_hash=route_hash)
    if auth.state != "active" and auth.mode != "vacant":
        return False, f"目标群权威状态为 {auth.state}，不可申请独占", None
    if auth.mode == "exclusive_clone":
        return _claim_existing_exclusive(session, auth, writer_kind=writer_kind, writer_id=writer_id)
    if auth.mode == "shared":
        return _claim_shared(session, auth, writer_kind=writer_kind, writer_id=writer_id, route_hash=route_hash)
    if auth.mode == "vacant":
        return _claim_vacant(session, auth, writer_kind=writer_kind, writer_id=writer_id, route_hash=route_hash)
    return False, f"目标群当前权威模式为 {auth.mode}，不可申请独占", None


def _lock_authority_scope(session, tenant_id, *, target_peer_type, target_peer_id) -> None:
    if not session.bind or session.bind.dialect.name != "postgresql":
        return
    raw = f"{tenant_id}:{target_peer_type}:{target_peer_id}".encode()
    lock_key = int.from_bytes(hashlib.sha256(raw).digest()[:8], "big", signed=True)
    session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key})


def _locked_authority(session, tenant_id, *, target_peer_type, target_peer_id):
    return session.scalar(select(TelegramGroupMutationAuthority).where(
        TelegramGroupMutationAuthority.tenant_id == tenant_id,
        TelegramGroupMutationAuthority.target_peer_type == target_peer_type,
        TelegramGroupMutationAuthority.target_peer_id == target_peer_id,
    ).with_for_update())


def _new_holder(auth, writer_kind, *, writer_id, route_hash, role="primary"):
    return TelegramGroupMutationAuthorityHolder(
        authority_id=auth.id, writer_kind=writer_kind, writer_id=writer_id,
        route_hash=route_hash, holder_role=role, state="active", version=1,
    )


def _claim_new(session, tenant_id, *, target_peer_type, target_peer_id, writer_kind, writer_id, route_hash):
    auth = TelegramGroupMutationAuthority(
        tenant_id=tenant_id, target_peer_type=target_peer_type, target_peer_id=target_peer_id,
        mode="exclusive_clone", gateway_admission_side="new", state="active", version=1,
    )
    session.add(auth)
    session.flush()
    session.add(_new_holder(auth, writer_kind, writer_id=writer_id, route_hash=route_hash))
    session.flush()
    return True, "", auth


def _claim_existing_exclusive(session, auth, *, writer_kind, writer_id):
    holders = session.scalars(select(TelegramGroupMutationAuthorityHolder).where(
        TelegramGroupMutationAuthorityHolder.authority_id == auth.id,
        TelegramGroupMutationAuthorityHolder.state == "active",
    )).all()
    if any(row.writer_kind == writer_kind and row.writer_id == writer_id for row in holders):
        return True, "", auth
    owner = holders[0].writer_id if holders else "unknown"
    return False, f"目标群已被其他克隆任务 ({owner}) 独占", None


def _claim_shared(session, auth, *, writer_kind, writer_id, route_hash):
    holders = session.scalars(select(TelegramGroupMutationAuthorityHolder).where(
        TelegramGroupMutationAuthorityHolder.authority_id == auth.id,
        TelegramGroupMutationAuthorityHolder.state == "active",
    )).all()
    others = [row for row in holders if (row.writer_kind, row.writer_id) != (writer_kind, writer_id)]
    if others:
        names = [f"{row.writer_kind}:{row.writer_id}" for row in others]
        return False, f"目标群存在其他活动中的平台写入者: {names}", None
    auth.mode, auth.gateway_admission_side, auth.version = "exclusive_clone", "new", auth.version + 1
    if not holders:
        session.add(_new_holder(auth, writer_kind, writer_id=writer_id, route_hash=route_hash))
    session.flush()
    return True, "", auth


def _claim_vacant(session, auth, *, writer_kind, writer_id, route_hash):
    auth.mode, auth.gateway_admission_side, auth.state = "exclusive_clone", "new", "active"
    auth.version += 1
    session.add(_new_holder(auth, writer_kind, writer_id=writer_id, route_hash=route_hash))
    session.flush()
    return True, "", auth


def release_exclusive_authority(
    session: Session,
    tenant_id: int,
    *,
    target_peer_type: str,
    target_peer_id: str,
    writer_kind: str,
    writer_id: str,
) -> bool:
    """
    释放独占写权限（转为 vacant / shared）。
    """
    stmt = (
        select(TelegramGroupMutationAuthority)
        .where(
            TelegramGroupMutationAuthority.tenant_id == tenant_id,
            TelegramGroupMutationAuthority.target_peer_type == target_peer_type,
            TelegramGroupMutationAuthority.target_peer_id == target_peer_id,
        )
        .with_for_update()
    )
    auth = session.execute(stmt).scalar_one_or_none()
    if not auth:
        return False

    holder_stmt = select(TelegramGroupMutationAuthorityHolder).where(
        TelegramGroupMutationAuthorityHolder.authority_id == auth.id,
        TelegramGroupMutationAuthorityHolder.writer_kind == writer_kind,
        TelegramGroupMutationAuthorityHolder.writer_id == writer_id,
        TelegramGroupMutationAuthorityHolder.state == "active",
    )
    holder = session.execute(holder_stmt).scalar_one_or_none()
    if holder:
        holder.state = "released"
        holder.version += 1

    # 检查是否还有其他活动 holder
    remaining_stmt = select(TelegramGroupMutationAuthorityHolder).where(
        TelegramGroupMutationAuthorityHolder.authority_id == auth.id,
        TelegramGroupMutationAuthorityHolder.state == "active",
    )
    remaining = session.execute(remaining_stmt).scalars().all()
    if not remaining:
        auth.mode = "vacant"
        auth.gateway_admission_side = "none"
    auth.version += 1
    session.flush()
    return True


def verify_gateway_admission(
    session: Session,
    tenant_id: int,
    *,
    target_peer_type: str,
    target_peer_id: str,
    writer_kind: str,
    writer_id: str,
) -> Tuple[bool, str]:
    """
    Gateway 调用前严格门禁：校验当前 writer 是否具备合法的 Admission 写入权。
    """
    stmt = select(TelegramGroupMutationAuthority).where(
        TelegramGroupMutationAuthority.tenant_id == tenant_id,
        TelegramGroupMutationAuthority.target_peer_type == target_peer_type,
        TelegramGroupMutationAuthority.target_peer_id == target_peer_id,
        TelegramGroupMutationAuthority.state == "active",
    )
    auth = session.execute(stmt).scalar_one_or_none()
    if not auth:
        return False, "目标群尚未登记平台写入权威，Gateway fail-closed"

    if auth.mode == "exclusive_clone":
        holder_stmt = select(TelegramGroupMutationAuthorityHolder).where(
            TelegramGroupMutationAuthorityHolder.authority_id == auth.id,
            TelegramGroupMutationAuthorityHolder.writer_kind == writer_kind,
            TelegramGroupMutationAuthorityHolder.writer_id == writer_id,
            TelegramGroupMutationAuthorityHolder.state == "active",
        )
        holder = session.execute(holder_stmt).scalar_one_or_none()
        if not holder:
            return False, f"目标群已被独占克隆锁定，拒绝 {writer_kind}:{writer_id} 写入"
        return True, ""

    if auth.mode == "handoff":
        return _verify_handoff_admission(
            session, auth, writer_kind=writer_kind, writer_id=writer_id,
        )

    if auth.mode == "vacant":
        return False, "目标群写权限处于 vacant 状态，需先 claim"

    return True, ""


def _verify_handoff_admission(session, authority, *, writer_kind, writer_id):
    if authority.gateway_admission_side == "none":
        return False, "目标群处于割接静默期，禁止任何写入"
    expected_role = {
        "new": "new_handoff",
        "old": "old_handoff",
    }.get(authority.gateway_admission_side)
    if expected_role is None:
        return False, "目标群 handoff admission side 无效"
    holder = session.scalar(select(TelegramGroupMutationAuthorityHolder).where(
        TelegramGroupMutationAuthorityHolder.authority_id == authority.id,
        TelegramGroupMutationAuthorityHolder.writer_kind == writer_kind,
        TelegramGroupMutationAuthorityHolder.writer_id == writer_id,
        TelegramGroupMutationAuthorityHolder.holder_role == expected_role,
        TelegramGroupMutationAuthorityHolder.state == "active",
    ))
    if holder is None:
        return False, f"目标群割接中，{writer_kind}:{writer_id} 不属于当前写入侧"
    return True, ""


def ensure_legacy_shared_holder(
    session: Session,
    tenant_id: int,
    *,
    target_peer_type: str, target_peer_id: str,
    writer_kind: str, writer_id: str,
    route_hash: str,
) -> TelegramGroupMutationAuthorityHolder:
    """为存量群写任务确保 shared authority holder 存在。"""
    stmt = (
        select(TelegramGroupMutationAuthority)
        .where(
            TelegramGroupMutationAuthority.tenant_id == tenant_id,
            TelegramGroupMutationAuthority.target_peer_type == target_peer_type,
            TelegramGroupMutationAuthority.target_peer_id == target_peer_id,
        )
        .with_for_update()
    )
    auth = session.execute(stmt).scalar_one_or_none()
    if not auth:
        auth = _new_shared_authority(
            session, tenant_id,
            target_peer_type=target_peer_type, target_peer_id=target_peer_id,
        )
    elif auth.mode != "shared" or auth.state != "active":
        raise ValueError(
            f"目标群 authority={auth.mode}/{auth.state}，禁止登记 shared writer"
        )
    holder = _shared_holder(
        session, auth.id, writer_kind=writer_kind, writer_id=writer_id,
    )
    if not holder:
        holder = TelegramGroupMutationAuthorityHolder(
            authority_id=auth.id,
            writer_kind=writer_kind,
            writer_id=writer_id,
            route_hash=route_hash,
            holder_role="shared_member",
            state="active",
            version=1,
        )
        session.add(holder)
        session.flush()
    elif holder.state != "active":
        holder.state = "active"
        holder.holder_role = "shared_member"
        holder.version += 1
    return holder


def _new_shared_authority(session, tenant_id, *, target_peer_type, target_peer_id):
    authority = TelegramGroupMutationAuthority(
        tenant_id=tenant_id,
        target_peer_type=target_peer_type,
        target_peer_id=target_peer_id,
        mode="shared",
        gateway_admission_side="all",
        state="active",
        version=1,
    )
    session.add(authority)
    session.flush()
    return authority


def _shared_holder(session, authority_id, *, writer_kind, writer_id):
    return session.scalar(select(TelegramGroupMutationAuthorityHolder).where(
        TelegramGroupMutationAuthorityHolder.authority_id == authority_id,
        TelegramGroupMutationAuthorityHolder.writer_kind == writer_kind,
        TelegramGroupMutationAuthorityHolder.writer_id == writer_id,
    ))


def ensure_platform_writer_admission(
    session: Session,
    tenant_id: int,
    *,
    target_peer_type: str,
    target_peer_id: str,
    writer_kind: str,
    writer_id: str,
) -> Tuple[bool, str]:
    _lock_authority_scope(
        session, tenant_id,
        target_peer_type=target_peer_type, target_peer_id=target_peer_id,
    )
    authority = _locked_authority(
        session, tenant_id,
        target_peer_type=target_peer_type, target_peer_id=target_peer_id,
    )
    if authority is None or authority.mode == "shared":
        route_hash = compute_route_hash(
            writer_kind, writer_id,
            target_peer_type=target_peer_type, target_peer_id=target_peer_id,
        )
        ensure_legacy_shared_holder(
            session, tenant_id,
            target_peer_type=target_peer_type,
            target_peer_id=target_peer_id,
            writer_kind=writer_kind,
            writer_id=writer_id,
            route_hash=route_hash,
        )
    return verify_gateway_admission(
        session, tenant_id,
        target_peer_type=target_peer_type,
        target_peer_id=target_peer_id,
        writer_kind=writer_kind,
        writer_id=writer_id,
    )


def release_platform_writer_admission(
    session: Session,
    tenant_id: int,
    *,
    target_peer_type: str,
    target_peer_id: str,
    writer_kind: str,
    writer_id: str,
) -> None:
    """释放一次性 shared writer；独占或 handoff 权威不得由这里改写。"""
    authority = _locked_authority(
        session,
        tenant_id,
        target_peer_type=target_peer_type,
        target_peer_id=target_peer_id,
    )
    if authority is None or authority.mode != "shared":
        return
    holder = session.scalar(select(TelegramGroupMutationAuthorityHolder).where(
        TelegramGroupMutationAuthorityHolder.authority_id == authority.id,
        TelegramGroupMutationAuthorityHolder.writer_kind == writer_kind,
        TelegramGroupMutationAuthorityHolder.writer_id == writer_id,
        TelegramGroupMutationAuthorityHolder.state == "active",
    ).with_for_update())
    if holder is None:
        return
    holder.state = "released"
    holder.version += 1
    authority.version += 1
