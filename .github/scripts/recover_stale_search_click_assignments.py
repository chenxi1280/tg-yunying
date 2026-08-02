from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os

from sqlalchemy import case, func, select

from app.database import SessionLocal
from app.models import (
    AccountEnvironmentBinding,
    AccountProxyBinding,
    Action,
    ExecutionAttempt,
    SearchClickFulfillmentObligation,
    SearchClickOpportunityAssignment,
    Task,
    TaskDayLedger,
)
from app.services._common import audit
from app.services.task_center.search_click_assignment_release import (
    release_search_click_assignment,
)


RELEASABLE_ASSIGNMENT_STATES = frozenset({"reserved", "action_bound", "claimed"})
RELEASABLE_ACTION_STATUSES = frozenset(
    {"pending", "claiming", "executing", "skipped", "failed"}
)
SCOPE_MISMATCH = "search_join_proxy_binding_scope_mismatch"
RELEASE_REASON = "search_assignment_pre_gateway_terminal"
SAMPLE_LIMIT = 30


def _required(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name.lower()}_required")
    return value


def _positive_int(value: object) -> int:
    try:
        parsed = int(str(value or "0"))
    except ValueError:
        return 0
    return parsed if parsed > 0 else 0


def _latest_ledger(session, task_id: str) -> TaskDayLedger:
    ledger = session.scalar(
        select(TaskDayLedger)
        .where(TaskDayLedger.task_id == task_id)
        .order_by(TaskDayLedger.period_start_at.desc())
        .limit(1)
    )
    if ledger is None:
        raise ValueError("search_click_task_day_ledger_missing")
    return ledger


def _attempt_stats(session, action_ids: tuple[str, ...]) -> dict[str, dict]:
    if not action_ids:
        return {}
    rows = session.execute(
        select(
            ExecutionAttempt.action_id,
            func.count(ExecutionAttempt.id),
            func.sum(case((ExecutionAttempt.gateway_call_started_at.is_not(None), 1), else_=0)),
            func.sum(case((ExecutionAttempt.failure_type == SCOPE_MISMATCH, 1), else_=0)),
        )
        .where(ExecutionAttempt.action_id.in_(action_ids))
        .group_by(ExecutionAttempt.action_id)
    )
    return {
        action_id: {
            "attempt_count": int(attempt_count or 0),
            "gateway_started_count": int(gateway_count or 0),
            "scope_mismatch_count": int(mismatch_count or 0),
        }
        for action_id, attempt_count, gateway_count, mismatch_count in rows
    }


def _by_id(session, model, ids: set) -> dict:
    if not ids:
        return {}
    return {row.id: row for row in session.scalars(select(model).where(model.id.in_(ids)))}


def _classification(assignment, action, gateway_started_count: int) -> str:
    if assignment.state == "released":
        return "already_released"
    action_allowed = action is None or action.status in RELEASABLE_ACTION_STATUSES
    if (
        assignment.state in RELEASABLE_ASSIGNMENT_STATES
        and action_allowed
        and gateway_started_count == 0
    ):
        return "releasable"
    return "precondition_lost"


def _candidate_item(
    assignment,
    action,
    *,
    route,
    environment,
    obligation,
    stats: dict,
) -> dict | None:
    payload = action.payload if action and isinstance(action.payload, dict) else {}
    runtime = payload.get("runtime_environment") or {}
    expected_binding_id = _positive_int(runtime.get("proxy_binding_id"))
    current_binding_id = int(environment.proxy_binding_id or 0) if environment else 0
    if not expected_binding_id or route is None or route.status != "replaced":
        return None
    if not current_binding_id or expected_binding_id == current_binding_id:
        return None
    gateway_count = int(stats.get("gateway_started_count") or 0)
    return {
        "assignment_id": assignment.id,
        "assignment_state": assignment.state,
        "assignment_version": assignment.version,
        "action_id": action.id if action else "",
        "action_status": action.status if action else "missing",
        "action_retry_count": int(action.retry_count or 0) if action else 0,
        "obligation_id": assignment.obligation_id,
        "obligation_status": obligation.status if obligation else "missing",
        "expected_binding_id": expected_binding_id,
        "current_binding_id": current_binding_id,
        "route_status": route.status,
        "attempt_count": int(stats.get("attempt_count") or 0),
        "gateway_started_count": gateway_count,
        "scope_mismatch_count": int(stats.get("scope_mismatch_count") or 0),
        "classification": _classification(assignment, action, gateway_count),
    }


def _scan(session, task_id: str) -> tuple[TaskDayLedger, list[dict]]:
    ledger = _latest_ledger(session, task_id)
    rows = list(
        session.execute(
            select(SearchClickOpportunityAssignment, Action)
            .join(Action, Action.id == SearchClickOpportunityAssignment.action_id)
            .where(
                SearchClickOpportunityAssignment.task_id == task_id,
                SearchClickOpportunityAssignment.task_day_ledger_id == ledger.id,
            )
        )
    )
    actions = {action.id: action for _assignment, action in rows}
    stats = _attempt_stats(session, tuple(actions))
    binding_ids, environment_ids, obligation_ids = _reference_ids(rows)
    bindings = _by_id(session, AccountProxyBinding, binding_ids)
    environments = _by_id(session, AccountEnvironmentBinding, environment_ids)
    obligations = _by_id(session, SearchClickFulfillmentObligation, obligation_ids)
    candidates = []
    for assignment, action in rows:
        runtime = (action.payload or {}).get("runtime_environment") or {}
        item = _candidate_item(
            assignment,
            action,
            route=bindings.get(_positive_int(runtime.get("proxy_binding_id"))),
            environment=environments.get(str(runtime.get("environment_binding_id") or "")),
            obligation=obligations.get(assignment.obligation_id),
            stats=stats.get(action.id, {}),
        )
        if item is not None:
            candidates.append(item)
    return ledger, sorted(candidates, key=lambda item: item["assignment_id"])


def _reference_ids(rows) -> tuple[set[int], set[str], set[str]]:
    binding_ids: set[int] = set()
    environment_ids: set[str] = set()
    obligation_ids: set[str] = set()
    for assignment, action in rows:
        runtime = (action.payload or {}).get("runtime_environment") or {}
        binding_id = _positive_int(runtime.get("proxy_binding_id"))
        environment_id = str(runtime.get("environment_binding_id") or "")
        if binding_id:
            binding_ids.add(binding_id)
        if environment_id:
            environment_ids.add(environment_id)
        obligation_ids.add(assignment.obligation_id)
    return binding_ids, environment_ids, obligation_ids


def _fingerprint(candidates: list[dict]) -> str:
    encoded = json.dumps(candidates, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _summary(mode: str, task_id: str, ledger, candidates: list[dict]) -> dict:
    classifications = Counter(item["classification"] for item in candidates)
    return {
        "mode": mode,
        "task_id": task_id,
        "ledger_id": ledger.id,
        "ledger_local_date": ledger.obligation_local_date.isoformat(),
        "ledger_status": ledger.lifecycle_status,
        "stale_count": len(candidates),
        "classification_counts": dict(sorted(classifications.items())),
        "scope_mismatch_attempt_count": sum(
            item["scope_mismatch_count"] for item in candidates
        ),
        "candidate_fingerprint": _fingerprint(candidates),
        "samples": candidates[:SAMPLE_LIMIT],
    }


def _current_candidate(session, assignment_id: str) -> dict | None:
    assignment = session.get(SearchClickOpportunityAssignment, assignment_id)
    action = session.get(Action, assignment.action_id) if assignment else None
    if assignment is None or action is None:
        return None
    runtime = (action.payload or {}).get("runtime_environment") or {}
    route = session.get(AccountProxyBinding, _positive_int(runtime.get("proxy_binding_id")))
    environment = session.get(
        AccountEnvironmentBinding,
        str(runtime.get("environment_binding_id") or ""),
    )
    obligation = session.get(SearchClickFulfillmentObligation, assignment.obligation_id)
    stats = _attempt_stats(session, (action.id,)).get(action.id, {})
    return _candidate_item(
        assignment,
        action,
        route=route,
        environment=environment,
        obligation=obligation,
        stats=stats,
    )


def _apply_one(task_id: str, item: dict, actor: str, approval_ref: str) -> dict:
    with SessionLocal() as session:
        current = _current_candidate(session, item["assignment_id"])
        if current is None or current["classification"] != "releasable":
            raise RuntimeError("stale_assignment_apply_precondition_changed")
        now_value = datetime.now(timezone.utc)
        trigger_hash = hashlib.sha256(approval_ref.encode()).hexdigest()[:12]
        batch = release_search_click_assignment(
            session,
            item["assignment_id"],
            trigger_key=f"operator_stale_binding:{trigger_hash}:{item['assignment_id']}",
            reason_code=RELEASE_REASON,
            now_value=now_value,
        )
        if batch.release_unit_count != 1:
            raise RuntimeError("stale_assignment_release_not_applied")
        audit(
            session,
            tenant_id=1,
            actor=actor,
            action="释放搜索点击旧代理绑定Assignment",
            target_type="search_click_opportunity_assignment",
            target_id=item["assignment_id"],
            detail=json.dumps({"approval_ref": approval_ref, "task_id": task_id}, sort_keys=True),
        )
        session.commit()
        return {"assignment_id": item["assignment_id"], "release_batch_id": batch.id}


def _wake_task(task_id: str, actor: str, approval_ref: str, released_count: int) -> None:
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise ValueError("search_click_task_not_found")
        now_value = datetime.now(timezone.utc)
        task.status = "running"
        task.next_run_at = now_value
        task.updated_at = now_value
        audit(
            session,
            tenant_id=task.tenant_id,
            actor=actor,
            action="唤醒搜索点击重新规划",
            target_type="task",
            target_id=task.id,
            detail=json.dumps(
                {"approval_ref": approval_ref, "released_count": released_count},
                sort_keys=True,
            ),
        )
        session.commit()


def _assert_apply_snapshot(summary: dict) -> None:
    expected_stale = int(_required("STALE_RECOVERY_EXPECTED_STALE_COUNT"))
    expected_releasable = int(_required("STALE_RECOVERY_EXPECTED_RELEASABLE_COUNT"))
    expected_fingerprint = _required("STALE_RECOVERY_EXPECTED_FINGERPRINT")
    actual_releasable = int(summary["classification_counts"].get("releasable", 0))
    if summary["stale_count"] != expected_stale:
        raise RuntimeError("stale_assignment_count_changed")
    if actual_releasable != expected_releasable:
        raise RuntimeError("stale_assignment_releasable_count_changed")
    if summary["candidate_fingerprint"] != expected_fingerprint:
        raise RuntimeError("stale_assignment_fingerprint_changed")


def main() -> None:
    task_id = _required("STALE_RECOVERY_TASK_ID")
    actor = _required("STALE_RECOVERY_ACTOR")
    approval_ref = _required("STALE_RECOVERY_APPROVAL_REF")
    apply_mode = str(os.environ.get("STALE_RECOVERY_APPLY") or "false") == "true"
    with SessionLocal() as session:
        ledger, candidates = _scan(session, task_id)
        before = _summary("apply" if apply_mode else "preview", task_id, ledger, candidates)
    if not apply_mode:
        print("STALE_SEARCH_ASSIGNMENT_RECOVERY=" + json.dumps(before, sort_keys=True))
        return
    _assert_apply_snapshot(before)
    releasable = [item for item in candidates if item["classification"] == "releasable"]
    applied = [_apply_one(task_id, item, actor, approval_ref) for item in releasable]
    _wake_task(task_id, actor, approval_ref, len(applied))
    with SessionLocal() as session:
        ledger, candidates = _scan(session, task_id)
        after = _summary("post_apply", task_id, ledger, candidates)
    result = {"before": before, "applied": applied, "after": after}
    print("STALE_SEARCH_ASSIGNMENT_RECOVERY=" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
