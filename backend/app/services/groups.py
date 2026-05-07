from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import AccountStatus, Campaign, GroupArchive, GroupAuthStatus, TgAccount, TgGroup, TgGroupAccount
from app.schemas import GroupPolicyUpdate

from ._common import audit, require_tenant
from .verification import list_verification_tasks


__all__ = [
    "authorize_group",
    "filter_groups",
    "group_detail",
    "update_group_policy",
]


def update_group_policy(session: Session, group_id: int, payload: GroupPolicyUpdate, actor: str = "普通用户") -> TgGroup:
    group = session.get(TgGroup, group_id)
    if not group:
        raise ValueError("group not found")

    for key, value in payload.model_dump(exclude_unset=True).items():
        if key in {"id", "tenant_id", "created_at", "updated_at"}:
            continue
        setattr(group, key, value)
    audit(session, tenant_id=group.tenant_id, actor=actor, action="更新群运营配置", target_type="tg_group", target_id=str(group.id))
    session.commit()
    session.refresh(group)
    return group


def authorize_group(session: Session, group_id: int, auth_status: str, actor: str) -> TgGroup:
    group = session.get(TgGroup, group_id)
    if not group:
        raise ValueError("group not found")
    group.auth_status = auth_status
    group.can_send = auth_status == GroupAuthStatus.AUTHORIZED.value
    links = session.scalars(select(TgGroupAccount).where(TgGroupAccount.group_id == group.id)).all()
    for link in links:
        link.can_send = group.can_send
    audit(session, tenant_id=group.tenant_id, actor=actor, action="确认群授权状态", target_type="tg_group", target_id=str(group.id), detail=auth_status)
    session.commit()
    session.refresh(group)
    return group


def group_detail(session: Session, group_id: int) -> dict:
    group = session.get(TgGroup, group_id)
    if not group:
        raise ValueError("group not found")
    links = list(
        session.scalars(
            select(TgGroupAccount)
            .where(TgGroupAccount.tenant_id == group.tenant_id, TgGroupAccount.group_id == group.id)
            .order_by(TgGroupAccount.id.asc())
        )
    )
    accounts = []
    for link in links:
        account = session.get(TgAccount, link.account_id)
        if not account:
            continue
        accounts.append(
            {
                "id": account.id,
                "display_name": account.display_name,
                "username": account.username,
                "status": account.status,
                "health_score": account.health_score,
                "permission_label": link.permission_label,
                "can_send": link.can_send,
                "last_sent_at": link.last_sent_at,
            }
        )
    group_id_text = str(group.id)
    recent_campaigns = list(
        session.scalars(
            select(Campaign)
            .where(
                Campaign.tenant_id == group.tenant_id,
                or_(Campaign.group_id == group.id, Campaign.target_group_ids.like(f"%{group_id_text}%")),
            )
            .order_by(Campaign.id.desc())
            .limit(5)
        )
    )
    recent_archives = list(
        session.scalars(
            select(GroupArchive)
            .where(GroupArchive.tenant_id == group.tenant_id, GroupArchive.group_id == group.id)
            .order_by(GroupArchive.id.desc())
            .limit(5)
        )
    )
    verification_tasks = list_verification_tasks(session, group.tenant_id, group_id=group.id, limit=8)
    return {
        "group": group,
        "accounts": accounts,
        "recent_campaigns": recent_campaigns,
        "recent_archives": recent_archives,
        "verification_tasks": verification_tasks,
        "stats": {
            "available_accounts": sum(1 for item in accounts if item["can_send"] and item["status"] == AccountStatus.ACTIVE.value),
            "recent_campaigns": len(recent_campaigns),
            "archives": len(recent_archives),
            "verification_tasks": len(verification_tasks),
        },
    }


def filter_groups(session: Session, tenant_id: int, page: int, page_size: int, search: str | None, status: str | None) -> list[TgGroup]:
    require_tenant(session, tenant_id)
    stmt = select(TgGroup).where(TgGroup.tenant_id == tenant_id)
    if search:
        stmt = stmt.where(TgGroup.title.like(f"%{search}%"))
    if status:
        stmt = stmt.where(TgGroup.auth_status == status)
    return list(session.scalars(stmt.order_by(TgGroup.id).offset((page - 1) * page_size).limit(page_size)))
