from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Action, AuditLog, ExecutionAttempt, Task, TaskAccountDailyCoverage
from app.services._common import _now
from app.services.task_center.daily_coverage import release_generation_contract_blocker
from app.services.task_center.ai_backlog_abandonment import abandon_ai_historical_backlog
from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION
from app.services.task_center.ai_generation_worker import drain_ai_generation


LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
TASK_TYPE = "group_ai_chat"
TODAY_CUTOFF = datetime(2026, 9, 3, 0, 0, 0, tzinfo=LOCAL_TIMEZONE)
RECOVERABLE_ACTION_STATUSES = frozenset({"failed", "skipped"})


@dataclass(frozen=True)
class RecoveryRequest:
    task_ids: tuple[str, ...]
    blocker_code: str
    apply: bool
    expected_state_hash: str
    actor: str
    approval_ref: str


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def parse_request() -> RecoveryRequest:
    task_ids = tuple(dict.fromkeys(
        item.strip()
        for item in _required_env("AI_GENERATION_CONTRACT_RECOVERY_TASK_IDS").split(",")
        if item.strip()
    ))
    apply_value = os.getenv("AI_GENERATION_CONTRACT_RECOVERY_APPLY", "false").lower()
    if not task_ids:
        raise ValueError("at least one exact task id is required")
    if apply_value not in {"true", "false"}:
        raise ValueError("AI_GENERATION_CONTRACT_RECOVERY_APPLY must be true or false")
    request = RecoveryRequest(
        task_ids=task_ids,
        blocker_code=os.getenv("AI_GENERATION_CONTRACT_RECOVERY_BLOCKER_CODE", "historical_backlog").strip(),
        apply=apply_value == "true",
        expected_state_hash=os.getenv(
            "AI_GENERATION_CONTRACT_RECOVERY_EXPECTED_STATE_HASH", "",
        ).strip(),
        actor=_required_env("AI_GENERATION_CONTRACT_RECOVERY_ACTOR"),
        approval_ref=_required_env("AI_GENERATION_CONTRACT_RECOVERY_APPROVAL_REF"),
    )
    if request.apply and len(request.expected_state_hash) != 64:
        raise ValueError("expected state hash is required for apply")
    return request


def snapshot_hash(snapshot: dict) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tasks(session: Session, request: RecoveryRequest, *, lock: bool = False) -> list[Task]:
    if len(request.task_ids) == 1 and request.task_ids[0].lower() == "all":
        statement = (
            select(Task)
            .where(
                Task.type == TASK_TYPE,
                Task.status == "running",
                Task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION,
            )
            .order_by(Task.id)
        )
        tasks = list(session.scalars(statement))
        if not tasks:
            raise ValueError("No active group_ai_chat tasks found for 'all'")
        return tasks

    statement = select(Task).where(Task.id.in_(request.task_ids)).order_by(Task.id)
    tasks = list(session.scalars(statement))
    found = {task.id for task in tasks}
    missing = set(request.task_ids) - found
    if missing:
        raise ValueError(f"AI generation contract recovery task identity missing: {missing}")
    for task in tasks:
        if (
            task.type != TASK_TYPE
            or task.status != "running"
            or task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION
        ):
            raise ValueError(
                f"AI generation contract recovery task state invalid: {task.id} (status={task.status}, type={task.type}, contract={task.fulfillment_contract_version})"
            )
    return tasks


def _find_stale_actions(session: Session, task_ids: list[str]) -> list[Action]:
    stmt = (
        select(Action)
        .where(
            Action.task_id.in_(task_ids),
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status == "pending",
            Action.scheduled_at < TODAY_CUTOFF,
        )
        .order_by(Action.scheduled_at, Action.id)
    )
    return list(session.scalars(stmt))


def recovery_snapshot(
    session: Session,
    request: RecoveryRequest,
    *,
    lock: bool = False,
) -> dict:
    tasks = _tasks(session, request, lock=lock)
    task_ids = [t.id for t in tasks]
    stale_actions = _find_stale_actions(session, task_ids)
    
    stale_by_task: dict[str, int] = {}
    for a in stale_actions:
        stale_by_task[a.task_id] = stale_by_task.get(a.task_id, 0) + 1

    return {
        "task_ids": task_ids,
        "task_names": [task.name for task in tasks],
        "blocker_code": request.blocker_code,
        "cutoff": TODAY_CUTOFF.isoformat(),
        "matched_count": len(stale_actions),
        "backlog_summary": stale_by_task,
        "sample_stale_ids": [a.id for a in stale_actions[:10]],
    }


def _sync_all_task_policies(session: Session, tasks: list[Task]) -> int:
    from app.models import AiContentPolicyVersion, TaskAiContentPolicyBinding
    from app.services.task_center.ai_content_policy import (
        AttestationSpec,
        TaskBindingSpec,
        bind_task_policy,
        create_adult_attestation,
    )
    
    policy = session.scalar(
        select(AiContentPolicyVersion)
        .where(AiContentPolicyVersion.status == "active")
        .order_by(AiContentPolicyVersion.version.desc())
    )
    if not policy:
        return 0

    count = 0
    for t in tasks:
        is_edu = any(k in t.name for k in ("大学", "师范", "学生会", "音乐"))
        group_id = str(t.type_config.get("target_group_id") or "")
        
        # Clean up existing binding for this (epoch, rev)
        session.execute(
            delete(TaskAiContentPolicyBinding).where(
                TaskAiContentPolicyBinding.task_id == t.id,
                TaskAiContentPolicyBinding.task_lifecycle_epoch == t.task_lifecycle_epoch,
                TaskAiContentPolicyBinding.task_config_revision == t.config_revision,
            )
        )
        
        if is_edu or not group_id:
            # Education / General group
            spec = TaskBindingSpec(
                task_id=t.id,
                policy_version_id=policy.id,
                allowed_routes=("general",),
                attestation_ids=(),
                scope_refs=(),
                approved_by="system_recovery",
            )
            bind_task_policy(session, spec)
        else:
            # Adult group
            att_ids = []
            for sub_class in ("adult_service", "adult_visual"):
                evidence_codes = (
                    ("adult_service_subject_verified", "adult_service_listing_verified")
                    if sub_class == "adult_service"
                    else ("adult_visual_content_verified",)
                )
                att_spec = AttestationSpec(
                    tenant_id=t.tenant_id,
                    scope_type="task_group",
                    scope_id=group_id,
                    subject_class=sub_class,
                    evidence_codes=evidence_codes,
                    actor_user_id=1,
                    permission_snapshot={"adult_content_attest": True, "role": "admin"},
                    expires_at=datetime(2027, 1, 1, tzinfo=LOCAL_TIMEZONE),
                    task_config_revision=t.config_revision,
                    policy_version=policy.version,
                )
                att = create_adult_attestation(session, att_spec)
                att_ids.append(att.id)
            
            spec = TaskBindingSpec(
                task_id=t.id,
                policy_version_id=policy.id,
                allowed_routes=("general", "adult_service_sensory", "adult_service_inquiry", "adult_visual"),
                attestation_ids=tuple(att_ids),
                scope_refs=(("task_group", group_id),),
                approved_by="system_recovery",
            )
            bind_task_policy(session, spec)
        count += 1
    session.flush()
    return count


def apply_recovery(session: Session, request: RecoveryRequest) -> tuple[dict, int]:
    snapshot = recovery_snapshot(session, request, lock=False)
    state_hash = snapshot_hash(snapshot)
    if state_hash != request.expected_state_hash:
        raise RuntimeError(f"State hash mismatch: expected {request.expected_state_hash}, got {state_hash}")
    
    tasks = _tasks(session, request, lock=False)
    task_ids = [t.id for t in tasks]
    timestamp = _now()
    
    # 1. Clean up stale actions and associated jobs
    stale_actions = _find_stale_actions(session, task_ids)
    deleted_count = 0
    if stale_actions:
        from app.models import GenerationJob
        stale_ids = [a.id for a in stale_actions]
        session.execute(
            delete(GenerationJob).where(
                GenerationJob.obligation_type == "action",
                GenerationJob.obligation_id.in_(stale_ids),
            )
        )
        del_res = session.execute(
            delete(Action).where(Action.id.in_(stale_ids))
        )
        deleted_count = int(del_res.rowcount or 0)

    # 2. Re-bind all task policies with valid attestations
    bindings_synced = _sync_all_task_policies(session, tasks)

    # 3. Wake tasks
    for task in tasks:
        task.next_run_at = timestamp
        task.updated_at = timestamp

    # 4. Audit log
    for task in tasks:
        session.add(AuditLog(
            tenant_id=task.tenant_id,
            actor=request.actor,
            action="AI活群策略绑定与生成通道恢复",
            target_type="task_group_ai_backlog_batch",
            target_id=task.id,
            detail=json.dumps({
                "approval_ref": request.approval_ref,
                "cutoff": TODAY_CUTOFF.isoformat(),
                "deleted_count": deleted_count,
                "bindings_synced": bindings_synced,
                "preview_state_hash": state_hash,
            }, ensure_ascii=False, sort_keys=True),
        ))
    session.commit()
    return snapshot, deleted_count


def _readback(deleted_count: int, request: RecoveryRequest) -> dict:
    with SessionLocal() as session:
        tasks = _tasks(session, request, lock=False)
        remaining = recovery_snapshot(session, request)
        
        # Check today's fresh pending actions
        fresh_stats = dict(session.execute(
            select(
                func.count(Action.id).label("fresh_pending"),
                func.min(Action.scheduled_at).label("min_sched"),
                func.max(Action.scheduled_at).label("max_sched"),
            ).where(
                Action.task_id.in_(request.task_ids if request.task_ids[0] != "all" else [t.id for t in tasks]),
                Action.status == "pending",
                Action.task_type == "group_ai_chat",
            )
        ).mappings().first() or {})

        return {
            "deleted_backlog_count": deleted_count,
            "remaining_backlog_count": remaining["matched_count"],
            "today_fresh_actions": {
                "count": fresh_stats.get("fresh_pending", 0),
                "min_sched": str(fresh_stats.get("min_sched", "")),
                "max_sched": str(fresh_stats.get("max_sched", "")),
            },
            "task_states": [{
                "task_id": task.id,
                "name": task.name,
                "status": task.status,
                "next_run_at": str(task.next_run_at),
            } for task in tasks],
        }


def main() -> int:
    request = parse_request()
    if not request.apply:
        with SessionLocal() as session:
            snapshot = recovery_snapshot(session, request)
        print("AI_GENERATION_CONTRACT_RECOVERY_PREVIEW=" + json.dumps({
            "mode": "preview",
            "snapshot": snapshot,
            "state_hash": snapshot_hash(snapshot),
        }, ensure_ascii=False, sort_keys=True, default=str))
        return 0
    with SessionLocal() as session:
        snapshot, deleted_count = apply_recovery(session, request)
    readback_data = _readback(deleted_count, request)
    print("AI_GENERATION_CONTRACT_RECOVERY_APPLY=" + json.dumps({
        "mode": "apply",
        "preview_state_hash": snapshot_hash(snapshot),
        "deleted_count": deleted_count,
        "readback": readback_data,
    }, ensure_ascii=False, sort_keys=True, default=str))
    
    # Trigger AI generation drain immediately
    try:
        drained = drain_ai_generation(SessionLocal, limit=100)
        print(f"IMMEDIATE_AI_GENERATION_DRAIN_PROCESSED={drained}")
    except Exception as exc:
        import traceback
        print(f"IMMEDIATE_AI_GENERATION_DRAIN_EXCEPTION={exc}\n{traceback.format_exc()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
