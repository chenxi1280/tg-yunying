from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import AccountCloneItem, AccountClonePlan, AccountStatus, TgAccount
from app.schemas import AccountClonePlanCreate

from ._common import _now, audit, gateway
from .developer_apps import credentials_for_account
from .accounts import account_contacts, account_groups


__all__ = [
    "account_clone_plan_detail",
    "account_clone_plans",
    "confirm_account_clone_plan",
    "create_account_clone_plan",
    "execute_clone_item",
    "refresh_clone_plan_counts",
    "retry_account_clone_item",
]


def account_clone_plans(session: Session, tenant_id: int, account_id: int | None = None, limit: int = 50) -> list[dict]:
    stmt = select(AccountClonePlan).where(AccountClonePlan.tenant_id == tenant_id)
    if account_id:
        plan_ids_from_items = select(AccountCloneItem.plan_id).where(AccountCloneItem.target_account_id == account_id)
        stmt = stmt.where(or_(AccountClonePlan.source_account_id == account_id, AccountClonePlan.target_account_id == account_id, AccountClonePlan.id.in_(plan_ids_from_items)))
    plans = list(session.scalars(stmt.order_by(AccountClonePlan.id.desc()).limit(limit)))
    return [account_clone_plan_detail(session, plan.id) for plan in plans]


def account_clone_plan_detail(session: Session, plan_id: int) -> dict:
    plan = session.get(AccountClonePlan, plan_id)
    if not plan:
        raise ValueError("clone plan not found")
    items = list(
        session.scalars(
            select(AccountCloneItem)
            .where(AccountCloneItem.tenant_id == plan.tenant_id, AccountCloneItem.plan_id == plan.id)
            .order_by(AccountCloneItem.id.asc())
        )
    )
    target_account_ids = sorted({item.target_account_id for item in items} or ({plan.target_account_id} if plan.target_account_id else set()))
    target_accounts_summary = []
    for target_id in target_account_ids:
        target = session.get(TgAccount, target_id)
        target_items = [item for item in items if item.target_account_id == target_id]
        target_accounts_summary.append(
            {
                "id": target_id,
                "display_name": target.display_name if target else f"账号 {target_id}",
                "status": target.status if target else "未知",
                "items_total": len(target_items),
                "items_done": sum(1 for item in target_items if item.status == "已完成"),
                "items_failed": sum(1 for item in target_items if item.status in {"失败", "需人工处理"}),
            }
        )
    items_by_target = {str(target_id): [item for item in items if item.target_account_id == target_id] for target_id in target_account_ids}
    return {
        "id": plan.id,
        "tenant_id": plan.tenant_id,
        "source_account_id": plan.source_account_id,
        "target_account_id": plan.target_account_id,
        "target_account_ids": target_account_ids,
        "target_accounts_summary": target_accounts_summary,
        "clone_scope": plan.clone_scope,
        "status": plan.status,
        "items_total": plan.items_total,
        "items_done": plan.items_done,
        "items_failed": plan.items_failed,
        "failure_detail": plan.failure_detail,
        "created_by": plan.created_by,
        "created_at": plan.created_at,
        "confirmed_at": plan.confirmed_at,
        "items": items,
        "items_by_target": items_by_target,
    }


def create_account_clone_plan(session: Session, payload: AccountClonePlanCreate, actor: str) -> dict:
    source = session.get(TgAccount, payload.source_account_id)
    target_ids = list(dict.fromkeys([*payload.target_account_ids, *([payload.target_account_id] if payload.target_account_id else [])]))
    targets = [session.get(TgAccount, target_id) for target_id in target_ids]
    if not source or source.deleted_at is not None or not target_ids or any(target is None or target.deleted_at is not None or target.tenant_id != payload.tenant_id for target in targets):
        raise ValueError("source or target account not found")
    if source.tenant_id != payload.tenant_id:
        raise ValueError("source or target account not found")
    if source.id in target_ids:
        raise ValueError("source and target account must be different")
    scopes = set(payload.clone_scope or ["contacts", "groups"])
    plan = AccountClonePlan(
        tenant_id=payload.tenant_id,
        source_account_id=source.id,
        target_account_id=target_ids[0],
        clone_scope=",".join(sorted(scopes)),
        status="待确认",
        created_by=actor,
    )
    session.add(plan)
    session.flush()
    total = 0
    if "contacts" in scopes:
        contacts = account_contacts(session, source.id, limit=200)
        for target in targets:
            for contact in contacts:
                session.add(
                    AccountCloneItem(
                        tenant_id=source.tenant_id,
                        plan_id=plan.id,
                        source_account_id=source.id,
                        target_account_id=target.id,
                        target_type="private",
                        target_peer_id=f"@{contact.username}" if contact.username else contact.peer_id,
                        target_display=contact.display_name,
                        status="待确认",
                    )
                )
                total += 1
    if "groups" in scopes:
        source_groups = account_groups(session, source.id)
        for target in targets:
            for group in source_groups:
                session.add(
                    AccountCloneItem(
                        tenant_id=source.tenant_id,
                        plan_id=plan.id,
                        source_account_id=source.id,
                        target_account_id=target.id,
                        target_type=group["group_type"] if group["group_type"] in {"group", "channel"} else "group",
                        target_peer_id=group["tg_peer_id"],
                        target_display=group["title"],
                        status="待确认",
                    )
                )
                total += 1
    plan.items_total = total
    audit(session, tenant_id=plan.tenant_id, actor=actor, action="创建账号克隆计划", target_type="account_clone_plan", target_id=str(plan.id), detail=f"targets={len(target_ids)};items={total}")
    session.commit()
    return account_clone_plan_detail(session, plan.id)


def execute_clone_item(session: Session, item_id: int, actor: str) -> AccountCloneItem:
    item = session.get(AccountCloneItem, item_id)
    if not item:
        raise ValueError("clone item not found")
    target = session.get(TgAccount, item.target_account_id)
    if not target or target.deleted_at is not None:
        raise ValueError("target account not found")
    if target.status != AccountStatus.ACTIVE.value:
        item.status = "失败"
        item.failure_type = "账号不可用"
        item.failure_detail = "目标账号未在线或需要重新登录"
        item.executed_at = _now()
        session.commit()
        return item
    try:
        credentials = credentials_for_account(session, target)
        result = gateway.clone_contact_or_group(target.id, item.target_type, item.target_peer_id, target.session_ciphertext, credentials)
    except Exception as exc:  # noqa: BLE001 - operator-facing detail.
        result = type("Result", (), {"ok": False, "status": "失败", "failure_type": "执行异常", "detail": str(exc)})()
    item.status = result.status if result.ok else result.status
    item.failure_type = "" if result.ok else result.failure_type
    item.failure_detail = result.detail
    item.executed_at = _now()
    audit(session, tenant_id=item.tenant_id, actor=actor, action="执行账号克隆项", target_type="account_clone_item", target_id=str(item.id), detail=f"{item.status}:{item.failure_detail}")
    session.commit()
    session.refresh(item)
    return item


def refresh_clone_plan_counts(session: Session, plan: AccountClonePlan) -> AccountClonePlan:
    items = list(session.scalars(select(AccountCloneItem).where(AccountCloneItem.plan_id == plan.id)))
    plan.items_total = len(items)
    plan.items_done = sum(1 for item in items if item.status == "已完成")
    plan.items_failed = sum(1 for item in items if item.status in {"失败", "需人工处理"})
    if plan.items_total and plan.items_done == plan.items_total:
        plan.status = "已完成"
    elif plan.items_failed:
        plan.status = "部分失败"
    else:
        plan.status = "执行中" if any(item.status != "待确认" for item in items) else plan.status
    return plan


def confirm_account_clone_plan(session: Session, plan_id: int, actor: str) -> dict:
    plan = session.get(AccountClonePlan, plan_id)
    if not plan:
        raise ValueError("clone plan not found")
    plan.status = "执行中"
    plan.confirmed_at = _now()
    for item_id in session.scalars(select(AccountCloneItem.id).where(AccountCloneItem.plan_id == plan.id, AccountCloneItem.status.in_(["待确认", "失败"]))):
        execute_clone_item(session, item_id, actor)
    plan = session.get(AccountClonePlan, plan_id)
    refresh_clone_plan_counts(session, plan)
    audit(session, tenant_id=plan.tenant_id, actor=actor, action="确认账号克隆计划", target_type="account_clone_plan", target_id=str(plan.id))
    session.commit()
    return account_clone_plan_detail(session, plan.id)


def retry_account_clone_item(session: Session, item_id: int, actor: str) -> AccountCloneItem:
    item = execute_clone_item(session, item_id, actor)
    plan = session.get(AccountClonePlan, item.plan_id)
    if plan:
        refresh_clone_plan_counts(session, plan)
        session.commit()
    return item
