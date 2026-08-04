from __future__ import annotations

import argparse
import json

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    Action,
    AuditLog,
    ExecutionAttempt,
    FulfillmentFactProjectionState,
    FulfillmentRemoteFact,
    GatewayRequestEvidenceJournal,
    SearchClickAssignment,
    SearchClickFulfillmentObligation,
)
from app.services.task_center.search_click_safe_settlement import (
    SAFE_NOT_EXECUTED_FACT,
    settle_search_click_assignment_from_remote_fact,
)


def preview(task_id: str, assignment_ids: tuple[str, ...]) -> dict:
    with SessionLocal() as session:
        rows = _load_rows(session, task_id, assignment_ids)
        return {
            "task_id": task_id,
            "assignment_count": len(rows),
            "rows": [_row_output(row) for row in rows],
            "apply": False,
        }


def apply_reconciliation(
    task_id: str,
    assignment_ids: tuple[str, ...],
    *,
    actor: str,
    approval_ref: str,
) -> dict:
    _require_approval(actor, approval_ref)
    with SessionLocal() as session:
        rows = _load_rows(
            session,
            task_id,
            assignment_ids,
            require_exact_unknown=True,
        )
        changed = 0
        for row in rows:
            before = _row_output(row)
            if settle_search_click_assignment_from_remote_fact(
                session,
                row["action"],
                row["fact"].fact_kind,
            ):
                changed += 1
            after = _row_output(row)
            session.add(AuditLog(
                tenant_id=row["action"].tenant_id,
                actor=actor[:100],
                action="搜索点击安全未执行收口",
                target_type="search_click_assignment",
                target_id=row["assignment"].id,
                detail=json.dumps({
                    "approval_ref": approval_ref,
                    "fact_id": row["fact"].fact_id,
                    "attempt_id": row["attempt"].id,
                    "before": before,
                    "after": after,
                }, ensure_ascii=False, sort_keys=True),
            ))
        session.commit()
    result = preview(task_id, assignment_ids)
    result.update({
        "apply": True,
        "changed_rows": changed,
        "actor": actor,
        "approval_ref": approval_ref,
    })
    return result


def _load_rows(
    session,
    task_id: str,
    assignment_ids: tuple[str, ...],
    *,
    require_exact_unknown: bool = False,
) -> list[dict]:
    ids = tuple(dict.fromkeys(value.strip() for value in assignment_ids if value.strip()))
    if not ids:
        raise ValueError("search_click_reconcile_assignment_ids_required")
    assignments = list(session.scalars(select(SearchClickAssignment).where(
        SearchClickAssignment.task_id == task_id,
        SearchClickAssignment.id.in_(ids),
    )))
    if len(assignments) != len(ids):
        raise ValueError("search_click_reconcile_assignment_set_mismatch")
    unknown_count = session.scalar(select(func.count(SearchClickAssignment.id)).where(
        SearchClickAssignment.task_id == task_id,
        SearchClickAssignment.state == "gateway_unknown",
    )) or 0
    target_unknown_count = sum(
        assignment.state == "gateway_unknown" for assignment in assignments
    )
    if require_exact_unknown and int(unknown_count) != target_unknown_count:
        raise ValueError("search_click_reconcile_unknown_set_drifted")
    rows = [_load_row(session, task_id, assignment) for assignment in assignments]
    if len(rows) != len(ids):
        raise ValueError("search_click_reconcile_row_count_mismatch")
    return sorted(rows, key=lambda row: row["assignment"].id)


def _load_row(session, task_id: str, assignment: SearchClickAssignment) -> dict:
    action = session.get(Action, assignment.action_id)
    if action is None:
        raise ValueError("search_click_reconcile_action_missing")
    payload = dict(action.payload or {})
    obligation = session.get(
        SearchClickFulfillmentObligation,
        str(payload.get("search_click_obligation_id") or ""),
    )
    fact = session.scalar(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.action_id == action.id,
        FulfillmentRemoteFact.fact_kind == SAFE_NOT_EXECUTED_FACT,
    ).order_by(FulfillmentRemoteFact.observed_at.desc()).limit(1))
    attempt = session.get(ExecutionAttempt, fact.attempt_id) if fact else None
    journal = session.scalar(select(GatewayRequestEvidenceJournal).where(
        GatewayRequestEvidenceJournal.action_id == action.id,
        GatewayRequestEvidenceJournal.execution_attempt_id == (attempt.id if attempt else ""),
    ).limit(1))
    states = list(session.scalars(select(FulfillmentFactProjectionState).where(
        FulfillmentFactProjectionState.fact_id == (fact.fact_id if fact else ""),
    )))
    _validate_row(task_id, assignment, action, obligation, attempt, fact, journal, states)
    return {
        "assignment": assignment,
        "action": action,
        "obligation": obligation,
        "attempt": attempt,
        "fact": fact,
        "journal": journal,
        "states": states,
    }


def _validate_row(task_id, assignment, action, obligation, attempt, fact, journal, states) -> None:
    if (
        action.task_id != task_id
        or action.task_type != "search_click"
        or action.action_type not in {"search_join", "search_join_membership"}
    ):
        raise ValueError("search_click_reconcile_task_binding_invalid")
    if assignment.state not in {"gateway_unknown", SAFE_NOT_EXECUTED_FACT}:
        raise ValueError("search_click_reconcile_assignment_state_invalid")
    if obligation is None or obligation.id != assignment.obligation_id:
        raise ValueError("search_click_reconcile_obligation_binding_invalid")
    payload = dict(action.payload or {})
    if payload.get("search_click_assignment_id") != assignment.id:
        raise ValueError("search_click_reconcile_action_assignment_invalid")
    if payload.get("search_click_obligation_id") != obligation.id:
        raise ValueError("search_click_reconcile_action_obligation_invalid")
    if attempt is None or fact is None or journal is None:
        raise ValueError("search_click_reconcile_evidence_missing")
    if journal.state != "recorded" or journal.remote_mutation_state != "false":
        raise ValueError("search_click_reconcile_journal_not_safe")
    if journal.remote_message_id or journal.remote_fact_id:
        raise ValueError("search_click_reconcile_journal_has_remote_identity")
    if fact.action_id != action.id or fact.attempt_id != attempt.id:
        raise ValueError("search_click_reconcile_fact_binding_invalid")
    if {row.projection_kind for row in states} != {"obligation", "action", "task_read_model"}:
        raise ValueError("search_click_reconcile_projection_incomplete")
    if any(row.state != "projected" for row in states):
        raise ValueError("search_click_reconcile_projection_not_projected")


def _row_output(row: dict) -> dict:
    assignment = row["assignment"]
    action = row["action"]
    obligation = row["obligation"]
    return {
        "assignment_id": assignment.id,
        "assignment_state": assignment.state,
        "assignment_version": assignment.version,
        "action_id": action.id,
        "action_status": action.status,
        "obligation_id": obligation.id,
        "obligation_status": obligation.status,
        "source_action_id": obligation.source_action_id,
        "attempt_id": row["attempt"].id,
        "fact_id": row["fact"].fact_id,
        "journal_id": row["journal"].id,
    }


def _require_approval(actor: str, approval_ref: str) -> None:
    if not actor.strip() or not approval_ref.strip():
        raise ValueError("search_click_reconcile_actor_and_approval_required")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile safe-not-executed direct search assignments.")
    parser.add_argument("command", choices=("preview", "apply"))
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--assignment-id", action="append", required=True)
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-ref", default="")
    args = parser.parse_args(argv)
    ids = tuple(args.assignment_id)
    result = (
        preview(args.task_id, ids)
        if args.command == "preview"
        else apply_reconciliation(
            args.task_id,
            ids,
            actor=args.actor,
            approval_ref=args.approval_ref,
        )
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
