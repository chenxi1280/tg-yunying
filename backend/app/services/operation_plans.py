from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    AccountRuntimeSummary,
    OperationPlanGenerationRun,
    OperationPlanTarget,
    OperationPlanTaskLink,
    OperationPlanTemplate,
    OperationTarget,
    Task,
    TgGroup,
    TgGroupAccount,
)
from app.schemas.operation_plans import OperationPlanCreate, OperationPlanGenerateRequest, OperationPlanUpdate
from app.services._common import _now, audit
from app.services.task_center.stats import empty_stats


def list_operation_plans(session: Session, tenant_id: int) -> list[dict[str, Any]]:
    plans = list(
        session.scalars(
            select(OperationPlanTemplate)
            .where(OperationPlanTemplate.tenant_id == tenant_id)
            .order_by(OperationPlanTemplate.updated_at.desc(), OperationPlanTemplate.id.desc())
        )
    )
    return [_plan_payload(session, plan) for plan in plans]


def get_operation_plan(session: Session, tenant_id: int, plan_id: int) -> dict[str, Any]:
    return _plan_payload(session, _get_plan(session, tenant_id, plan_id))


def create_operation_plan(session: Session, tenant_id: int, payload: OperationPlanCreate, actor: str) -> dict[str, Any]:
    plan = OperationPlanTemplate(
        tenant_id=tenant_id,
        name=payload.name.strip(),
        description=payload.description.strip(),
        target_type=payload.target_type,
        strategy_config=payload.strategy_config,
        task_blueprints=payload.task_blueprints or _default_blueprints(payload.target_type),
        created_by=actor,
        updated_by=actor,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(plan)
    session.flush()
    _replace_plan_targets(session, tenant_id, plan, payload.target_ids)
    audit(session, tenant_id=tenant_id, actor=actor, action="创建运营方案", target_type="operation_plan", target_id=str(plan.id), detail=plan.name)
    session.commit()
    return _plan_payload(session, plan)


def update_operation_plan(session: Session, tenant_id: int, plan_id: int, payload: OperationPlanUpdate, actor: str) -> dict[str, Any]:
    plan = _get_plan(session, tenant_id, plan_id)
    data = payload.model_dump(exclude_unset=True)
    for key in ("name", "description", "target_type", "status", "strategy_config", "task_blueprints"):
        if key in data and data[key] is not None:
            value = data[key]
            if key in {"name", "description"}:
                value = str(value).strip()
            setattr(plan, key, value)
    if "target_ids" in data and data["target_ids"] is not None:
        _replace_plan_targets(session, tenant_id, plan, data["target_ids"])
    plan.updated_by = actor
    plan.updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="更新运营方案", target_type="operation_plan", target_id=str(plan.id), detail=plan.name)
    session.commit()
    return _plan_payload(session, plan)


def preview_operation_plan(session: Session, tenant_id: int, plan_id: int, payload: OperationPlanGenerateRequest, actor: str) -> dict[str, Any]:
    plan = _get_plan(session, tenant_id, plan_id)
    planned_tasks, blockers, warnings = _planned_tasks(session, tenant_id, plan, payload.target_ids)
    target_rows = _targets_for_plan(session, tenant_id, plan, payload.target_ids)
    preview = _preview_breakdown(session, tenant_id, target_rows, planned_tasks, blockers)
    run = _record_run(
        session,
        tenant_id,
        plan.id,
        "preview",
        actor,
        payload.model_dump(mode="json"),
        {**preview, "planned_tasks": planned_tasks, "blockers": blockers, "warnings": warnings},
    )
    session.commit()
    return {
        "plan_id": plan.id,
        "target_count": len({item["target_id"] for item in planned_tasks if item.get("target_id")}),
        "estimated_task_count": len(planned_tasks),
        "estimated_target_count": preview["estimated_target_count"],
        "account_capacity": preview["account_capacity"],
        "admission_actions": preview["admission_actions"],
        "target_previews": preview["target_previews"],
        "planned_tasks": planned_tasks,
        "blockers": blockers,
        "warnings": warnings,
        "run": run,
    }


def generate_operation_plan_tasks(session: Session, tenant_id: int, plan_id: int, payload: OperationPlanGenerateRequest, actor: str) -> dict[str, Any]:
    plan = _get_plan(session, tenant_id, plan_id)
    planned_tasks, blockers, warnings = _planned_tasks(session, tenant_id, plan, payload.target_ids)
    if blockers:
        run = _record_run(
            session,
            tenant_id,
            plan.id,
            "generate_tasks",
            actor,
            payload.model_dump(mode="json"),
            {"created_task_ids": [], "blockers": blockers, "warnings": warnings},
            status="blocked",
        )
        session.commit()
        return {"plan_id": plan.id, "created_task_ids": [], "linked_task_count": 0, "run": run}
    created_ids: list[str] = []
    for item in planned_tasks:
        task = Task(
            tenant_id=tenant_id,
            name=item["name"],
            type=item["task_type"],
            status="running" if payload.auto_start else "draft",
            priority=int(item.get("priority") or 3),
            timezone="Asia/Shanghai",
            next_run_at=_now() if payload.auto_start else None,
            account_config=item.get("account_config") or _default_account_config(),
            pacing_config=item.get("pacing_config") or _default_pacing_config(),
            failure_policy=item.get("failure_policy") or _default_failure_policy(),
            type_config=item.get("type_config") or {},
            stats=empty_stats(),
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(task)
        session.flush()
        created_ids.append(task.id)
        session.add(
            OperationPlanTaskLink(
                tenant_id=tenant_id,
                plan_id=plan.id,
                target_id=item.get("target_id"),
                task_id=task.id,
                relation="generated",
                status="active",
                created_at=_now(),
            )
        )
    run = _record_run(
        session,
        tenant_id,
        plan.id,
        "generate_tasks",
        actor,
        payload.model_dump(mode="json"),
        {"created_task_ids": created_ids, "blockers": blockers, "warnings": warnings, "auto_start": payload.auto_start},
    )
    audit(session, tenant_id=tenant_id, actor=actor, action="生成运营方案任务", target_type="operation_plan", target_id=str(plan.id), detail=f"tasks={len(created_ids)}; reason={payload.reason}")
    session.commit()
    return {"plan_id": plan.id, "created_task_ids": created_ids, "linked_task_count": len(created_ids), "run": run}


def apply_operation_plan_to_linked_tasks(session: Session, tenant_id: int, plan_id: int, payload: OperationPlanGenerateRequest, actor: str) -> dict[str, Any]:
    plan = _get_plan(session, tenant_id, plan_id)
    impact_preview = _linked_task_impact_preview(session, tenant_id, plan)
    if payload.confirm_apply and not payload.reason.strip():
        raise ValueError("应用关联任务调整必须填写原因")
    applied_task_ids: list[str] = []
    if payload.confirm_apply:
        for item in impact_preview["items"]:
            if not item.get("will_update") or not item.get("changed_fields"):
                continue
            task = session.get(Task, item["task_id"])
            if not task or task.tenant_id != tenant_id:
                continue
            after = item.get("after") or {}
            task.name = str(after.get("name") or task.name)
            task.priority = int(after.get("priority") or task.priority or 3)
            task.account_config = after.get("account_config") or {}
            task.pacing_config = after.get("pacing_config") or {}
            task.failure_policy = after.get("failure_policy") or {}
            task.type_config = after.get("type_config") or {}
            task.updated_at = _now()
            applied_task_ids.append(task.id)
    result_payload = {
        "linked_task_count": impact_preview["linked_task_count"],
        "applied_task_ids": applied_task_ids,
        "requires_confirmation": not payload.confirm_apply and impact_preview["requires_confirmation"],
        "impact_preview": impact_preview,
    }
    run = _record_run(
        session,
        tenant_id,
        plan.id,
        "apply_to_linked_tasks",
        actor,
        payload.model_dump(mode="json"),
        result_payload,
    )
    action = "应用运营方案关联任务调整" if payload.confirm_apply else "预览运营方案关联任务调整"
    audit(session, tenant_id=tenant_id, actor=actor, action=action, target_type="operation_plan", target_id=str(plan.id), detail=f"{payload.reason}; tasks={len(applied_task_ids)}")
    session.commit()
    return {
        "plan_id": plan.id,
        "created_task_ids": [],
        "linked_task_count": impact_preview["linked_task_count"],
        "applied_task_ids": applied_task_ids,
        "requires_confirmation": not payload.confirm_apply and impact_preview["requires_confirmation"],
        "impact_preview": impact_preview,
        "run": run,
    }


def pause_operation_plan(session: Session, tenant_id: int, plan_id: int, actor: str) -> dict[str, Any]:
    plan = _get_plan(session, tenant_id, plan_id)
    plan.status = "paused"
    plan.updated_by = actor
    plan.updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="暂停运营方案", target_type="operation_plan", target_id=str(plan.id), detail=plan.name)
    session.commit()
    return _plan_payload(session, plan)


def list_operation_plan_runs(session: Session, tenant_id: int, plan_id: int) -> list[OperationPlanGenerationRun]:
    _get_plan(session, tenant_id, plan_id)
    return list(
        session.scalars(
            select(OperationPlanGenerationRun)
            .where(OperationPlanGenerationRun.tenant_id == tenant_id, OperationPlanGenerationRun.plan_id == plan_id)
            .order_by(OperationPlanGenerationRun.created_at.desc())
        )
    )


def _get_plan(session: Session, tenant_id: int, plan_id: int) -> OperationPlanTemplate:
    plan = session.get(OperationPlanTemplate, plan_id)
    if not plan or plan.tenant_id != tenant_id:
        raise ValueError("operation plan not found")
    return plan


def _replace_plan_targets(session: Session, tenant_id: int, plan: OperationPlanTemplate, target_ids: list[int]) -> None:
    session.execute(delete(OperationPlanTarget).where(OperationPlanTarget.tenant_id == tenant_id, OperationPlanTarget.plan_id == plan.id))
    for target_id in sorted({int(item) for item in target_ids if item}):
        target = session.get(OperationTarget, target_id)
        if not target or target.tenant_id != tenant_id:
            raise ValueError(f"operation target not found: {target_id}")
        session.add(OperationPlanTarget(tenant_id=tenant_id, plan_id=plan.id, target_id=target_id, strategy_config={}, created_at=_now(), updated_at=_now()))


def _planned_tasks(session: Session, tenant_id: int, plan: OperationPlanTemplate, target_ids: list[int] | None) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    targets = _targets_for_plan(session, tenant_id, plan, target_ids)
    blockers: list[str] = []
    warnings: list[str] = []
    if not targets:
        blockers.append("未绑定运营目标")
        return [], blockers, warnings
    blueprints = plan.task_blueprints or _default_blueprints(plan.target_type)
    planned: list[dict[str, Any]] = []
    for target in targets:
        if target.auth_status not in {"已授权", "授权", "正常", "未确认"}:
            warnings.append(f"{target.title} 当前准入状态为 {target.auth_status}")
        for blueprint in blueprints:
            task_type = str(blueprint.get("task_type") or blueprint.get("type") or _default_task_type(target.target_type))
            item = _task_plan_item(target, task_type, blueprint)
            blockers.extend(_task_plan_item_blockers(item))
            planned.append(item)
    return planned, blockers, warnings


def _targets_for_plan(session: Session, tenant_id: int, plan: OperationPlanTemplate, target_ids: list[int] | None) -> list[OperationTarget]:
    ids = sorted({int(item) for item in (target_ids or []) if item})
    if not ids:
        ids = [item.target_id for item in _plan_targets(session, plan.id) if item.status == "active"]
    if not ids:
        return []
    return list(session.scalars(select(OperationTarget).where(OperationTarget.tenant_id == tenant_id, OperationTarget.id.in_(ids)).order_by(OperationTarget.id.asc())))


def _task_plan_item(target: OperationTarget, task_type: str, blueprint: dict[str, Any]) -> dict[str, Any]:
    type_config = dict(blueprint.get("type_config") or {})
    if target.target_type == "group":
        type_config.setdefault("target_operation_target_id", target.id)
        type_config.setdefault("target_type", "group")
        type_config.setdefault("target_group_name", target.title)
    else:
        type_config.setdefault("target_channel_id", target.id)
        type_config.setdefault("target_type", "channel")
        type_config.setdefault("target_channel_name", target.title)
    return {
        "target_id": target.id,
        "target_title": target.title,
        "task_type": task_type,
        "name": str(blueprint.get("name") or f"{target.title} {task_type}"),
        "priority": int(blueprint.get("priority") or 3),
        "account_config": blueprint.get("account_config") or _default_account_config(),
        "pacing_config": blueprint.get("pacing_config") or _default_pacing_config(),
        "failure_policy": blueprint.get("failure_policy") or _default_failure_policy(),
        "type_config": type_config,
    }


def _task_plan_item_blockers(item: dict[str, Any]) -> list[str]:
    task_type = str(item.get("task_type") or "")
    if task_type != "group_relay":
        return []
    config = item.get("type_config") or {}
    source_groups = config.get("source_groups")
    if isinstance(source_groups, list) and any(_valid_relay_source_group(source) for source in source_groups):
        return []
    target_title = str(item.get("target_title") or item.get("target_id") or "")
    return [f"{target_title} 转发监听任务缺少来源群"]


def _valid_relay_source_group(source: Any) -> bool:
    if not isinstance(source, dict):
        return False
    return bool(source.get("operation_target_id") or source.get("group_id"))


def _preview_breakdown(
    session: Session,
    tenant_id: int,
    targets: list[OperationTarget],
    planned_tasks: list[dict[str, Any]],
    blockers: list[str],
) -> dict[str, Any]:
    tasks_by_target: dict[int, list[dict[str, Any]]] = {}
    for task in planned_tasks:
        target_id = int(task.get("target_id") or 0)
        if target_id:
            tasks_by_target.setdefault(target_id, []).append(task)
    target_previews = [_target_preview(session, target, tasks_by_target.get(target.id, [])) for target in targets]
    return {
        "estimated_task_count": len(planned_tasks),
        "estimated_target_count": len(target_previews),
        "account_capacity": _account_capacity_preview(session, tenant_id),
        "admission_actions": _admission_action_preview(target_previews, planned_tasks),
        "target_previews": target_previews,
        "blocking_reasons": blockers,
    }


def _target_preview(session: Session, target: OperationTarget, planned_tasks: list[dict[str, Any]]) -> dict[str, Any]:
    linked_group = session.scalar(select(TgGroup).where(TgGroup.tenant_id == target.tenant_id, TgGroup.tg_peer_id == target.tg_peer_id))
    links = list(session.scalars(select(TgGroupAccount).where(TgGroupAccount.tenant_id == target.tenant_id, TgGroupAccount.group_id == linked_group.id))) if linked_group else []
    send_account_count = len([link for link in links if link.can_send])
    listener_account_count = len([link for link in links if link.is_listener])
    task_types = [str(task.get("task_type") or "") for task in planned_tasks]
    warnings: list[str] = []
    blockers: list[str] = []
    if target.target_type == "group" and not linked_group:
        blockers.append("目标群未建立本地群映射")
    if any(_task_requires_send(task_type) for task_type in task_types) and not target.can_send:
        blockers.append("目标不可发送")
    if any(_task_requires_send(task_type) for task_type in task_types) and target.target_type == "group" and send_account_count <= 0:
        blockers.append("没有可发送账号")
    if any(task_type == "group_relay" for task_type in task_types) and listener_account_count <= 0:
        warnings.append("缺少监听账号，转发监听任务可能无法启动")
    if target.auth_status not in {"已授权", "已授权运营", "授权", "正常", "未确认"}:
        warnings.append(f"目标准入状态为 {target.auth_status}")
    return {
        "target_id": target.id,
        "target_title": target.title,
        "target_type": target.target_type,
        "task_count": len(planned_tasks),
        "task_types": task_types,
        "auth_status": target.auth_status,
        "can_send": bool(target.can_send),
        "send_account_count": send_account_count,
        "listener_account_count": listener_account_count,
        "blockers": blockers,
        "warnings": warnings,
    }


def _account_capacity_preview(session: Session, tenant_id: int) -> dict[str, Any]:
    rows = list(session.scalars(select(AccountRuntimeSummary).where(AccountRuntimeSummary.tenant_id == tenant_id)))
    return {
        "summary_count": len(rows),
        "send_available": sum(1 for row in rows if row.send_available),
        "listen_available": sum(1 for row in rows if row.listen_available),
        "join_available": sum(1 for row in rows if row.join_available),
        "comment_available": sum(1 for row in rows if row.comment_available),
        "remaining_capacity": sum(int(row.remaining_capacity or 0) for row in rows),
        "stale_or_missing": len(rows) == 0,
    }


def _admission_action_preview(target_previews: list[dict[str, Any]], planned_tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    target_map = {int(item["target_id"]): item for item in target_previews}
    actions: list[dict[str, Any]] = []
    for task in planned_tasks:
        task_type = str(task.get("task_type") or "")
        target_id = int(task.get("target_id") or 0)
        target = target_map.get(target_id) or {}
        action_type = _admission_action_type(task_type, str(target.get("target_type") or ""))
        if not action_type:
            continue
        actions.append(
            {
                "target_id": target_id,
                "target_title": target.get("target_title") or task.get("target_title") or "",
                "task_type": task_type,
                "action_type": action_type,
                "required_accounts": _required_account_count(task),
                "blocking_reasons": list(target.get("blockers") or []),
                "warnings": list(target.get("warnings") or []),
            }
        )
    return actions


def _admission_action_type(task_type: str, target_type: str) -> str:
    if task_type in {"channel_view", "channel_like", "channel_comment"}:
        return "ensure_channel_membership"
    if task_type in {"group_ai_chat", "group_relay"} and target_type == "group":
        return "ensure_group_send_or_listen"
    return ""


def _task_requires_send(task_type: str) -> bool:
    return task_type in {"group_ai_chat", "group_relay", "channel_comment"}


def _required_account_count(task: dict[str, Any]) -> int:
    account_config = task.get("account_config") or {}
    account_ids = account_config.get("account_ids") or []
    if isinstance(account_ids, list) and account_ids:
        return len(account_ids)
    return int(account_config.get("max_concurrent") or 0)


def _linked_task_impact_preview(session: Session, tenant_id: int, plan: OperationPlanTemplate) -> dict[str, Any]:
    links = [link for link in _plan_links(session, plan.id) if link.status == "active"]
    blueprints = plan.task_blueprints or _default_blueprints(plan.target_type)
    items: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []
    for link in links:
        task = session.get(Task, link.task_id)
        if not task or task.tenant_id != tenant_id:
            blockers.append(f"关联任务不存在：{link.task_id}")
            continue
        target = session.get(OperationTarget, link.target_id) if link.target_id else None
        if target and target.tenant_id != tenant_id:
            target = None
        if not target:
            blockers.append(f"任务 {task.id} 缺少关联目标，不能从方案推导配置")
            items.append(_impact_item_for_blocked_task(task, link, "缺少关联目标"))
            continue
        blueprint = _blueprint_for_task_type(blueprints, task.type)
        if not blueprint:
            blockers.append(f"任务 {task.id} 类型 {task.type} 未在方案模板中配置")
            items.append(_impact_item_for_blocked_task(task, link, "任务类型未在方案模板中配置", target))
            continue
        before = _task_config_snapshot(task)
        after = _desired_task_config(task, target, blueprint)
        changed_fields = [field for field, value in after.items() if before.get(field) != value]
        will_update = bool(changed_fields) and task.status not in {"deleted", "completed", "stopped"}
        if changed_fields and not will_update:
            warnings.append(f"任务 {task.id} 状态为 {task.status}，不会自动更新")
        items.append(
            {
                "task_id": task.id,
                "task_name": task.name,
                "task_type": task.type,
                "task_status": task.status,
                "target_id": target.id,
                "target_title": target.title,
                "changed_fields": changed_fields,
                "will_update": will_update,
                "requires_confirmation": will_update and task.status in {"running", "pending", "paused"},
                "before": before,
                "after": after,
            }
        )
    return {
        "linked_task_count": len(links),
        "changed_task_count": sum(1 for item in items if item.get("will_update")),
        "running_task_count": sum(1 for item in items if item.get("will_update") and item.get("task_status") in {"running", "pending", "paused"}),
        "requires_confirmation": any(item.get("requires_confirmation") for item in items),
        "blockers": blockers,
        "warnings": warnings,
        "items": items,
    }


def _impact_item_for_blocked_task(task: Task, link: OperationPlanTaskLink, reason: str, target: OperationTarget | None = None) -> dict[str, Any]:
    snapshot = _task_config_snapshot(task)
    return {
        "task_id": task.id,
        "task_name": task.name,
        "task_type": task.type,
        "task_status": task.status,
        "target_id": target.id if target else link.target_id,
        "target_title": target.title if target else "",
        "changed_fields": [],
        "will_update": False,
        "requires_confirmation": False,
        "block_reason": reason,
        "before": snapshot,
        "after": snapshot,
    }


def _blueprint_for_task_type(blueprints: list[dict[str, Any]], task_type: str) -> dict[str, Any] | None:
    for blueprint in blueprints:
        if str(blueprint.get("task_type") or blueprint.get("type") or "") == task_type:
            return blueprint
    return None


def _task_config_snapshot(task: Task) -> dict[str, Any]:
    return {
        "name": task.name,
        "priority": int(task.priority or 3),
        "account_config": dict(task.account_config or {}),
        "pacing_config": dict(task.pacing_config or {}),
        "failure_policy": dict(task.failure_policy or {}),
        "type_config": dict(task.type_config or {}),
    }


def _desired_task_config(task: Task, target: OperationTarget, blueprint: dict[str, Any]) -> dict[str, Any]:
    planned = _task_plan_item(target, task.type, blueprint)
    current = _task_config_snapshot(task)
    type_config = {**current["type_config"], **dict(planned.get("type_config") or {})}
    return {
        "name": str(planned.get("name") or current["name"]) if "name" in blueprint else current["name"],
        "priority": int(planned.get("priority") or current["priority"] or 3) if "priority" in blueprint else current["priority"],
        "account_config": planned.get("account_config") or current["account_config"] if "account_config" in blueprint else current["account_config"],
        "pacing_config": planned.get("pacing_config") or current["pacing_config"] if "pacing_config" in blueprint else current["pacing_config"],
        "failure_policy": planned.get("failure_policy") or current["failure_policy"] if "failure_policy" in blueprint else current["failure_policy"],
        "type_config": type_config,
    }


def _record_run(
    session: Session,
    tenant_id: int,
    plan_id: int,
    run_type: str,
    actor: str,
    request_payload: dict[str, Any],
    result_payload: dict[str, Any],
    *,
    status: str = "success",
) -> OperationPlanGenerationRun:
    run = OperationPlanGenerationRun(
        tenant_id=tenant_id,
        plan_id=plan_id,
        run_type=run_type,
        status=status,
        requested_by=actor,
        request_payload=request_payload,
        result_payload=result_payload,
        created_at=_now(),
        finished_at=_now(),
    )
    session.add(run)
    session.flush()
    return run


def _plan_payload(session: Session, plan: OperationPlanTemplate) -> dict[str, Any]:
    targets = _plan_targets(session, plan.id)
    links = _plan_links(session, plan.id)
    latest_run = session.scalar(
        select(OperationPlanGenerationRun)
        .where(OperationPlanGenerationRun.tenant_id == plan.tenant_id, OperationPlanGenerationRun.plan_id == plan.id)
        .order_by(OperationPlanGenerationRun.created_at.desc())
        .limit(1)
    )
    return {
        "id": plan.id,
        "tenant_id": plan.tenant_id,
        "name": plan.name,
        "description": plan.description,
        "target_type": plan.target_type,
        "status": plan.status,
        "strategy_config": plan.strategy_config or {},
        "task_blueprints": plan.task_blueprints or [],
        "created_by": plan.created_by,
        "updated_by": plan.updated_by,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
        "targets": targets,
        "task_links": links,
        "latest_run": latest_run,
    }


def _plan_targets(session: Session, plan_id: int) -> list[OperationPlanTarget]:
    return list(session.scalars(select(OperationPlanTarget).where(OperationPlanTarget.plan_id == plan_id).order_by(OperationPlanTarget.id.asc())))


def _plan_links(session: Session, plan_id: int) -> list[OperationPlanTaskLink]:
    return list(session.scalars(select(OperationPlanTaskLink).where(OperationPlanTaskLink.plan_id == plan_id).order_by(OperationPlanTaskLink.created_at.desc())))


def _default_task_type(target_type: str) -> str:
    return "group_ai_chat" if target_type == "group" else "channel_view"


def _default_blueprints(target_type: str) -> list[dict[str, Any]]:
    if target_type == "channel":
        return [{"task_type": "channel_view", "name": "频道浏览"}, {"task_type": "channel_like", "name": "频道点赞"}]
    return [{"task_type": "group_ai_chat", "name": "群活跃暖场"}]


def _default_account_config() -> dict[str, Any]:
    return {"selection_mode": "all", "account_group_id": None, "account_ids": [], "max_concurrent": 20, "cooldown_per_account_minutes": 5, "ban_policy": "skip"}


def _default_pacing_config() -> dict[str, Any]:
    return {"mode": "template", "template": "moderate_6h", "jitter_percent": 30}


def _default_failure_policy() -> dict[str, Any]:
    return {"max_retries": 3, "retry_delay_seconds": 60, "retry_backoff": "exponential", "on_account_banned": "skip_account", "on_api_rate_limit": "wait_and_retry", "on_content_rejected": "skip_message", "alert_on_failure": False}


__all__ = [
    "apply_operation_plan_to_linked_tasks",
    "create_operation_plan",
    "generate_operation_plan_tasks",
    "get_operation_plan",
    "list_operation_plan_runs",
    "list_operation_plans",
    "pause_operation_plan",
    "preview_operation_plan",
    "update_operation_plan",
]
