from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    Action,
    AuditLog,
    ExecutionAttempt,
    FulfillmentRemoteFact,
    GatewayRequestEvidenceJournal,
    SearchClickAssignment,
    SearchClickFulfillmentObligation,
)
from app.services._common import _now
from app.services.task_center.dispatcher import _finalize_fact_first_dispatch
from app.services.task_center.search_click_safe_settlement import (
    SAFE_NOT_EXECUTED_FACT,
)


PRE_ACCEPT_REASONS = frozenset({
    "verification_ai_unavailable",
    "verification_consensus_unavailable",
    "verification_deadline_exceeded",
    "verification_local_ocr_timeout",
    "verification_refresh_transport_unavailable",
    "verification_refresh_unexpected_page",
    "verification_transport_unavailable",
})
PRE_ACCEPT_ERROR_CODES = frozenset({
    "bot_human_verification_required",
    "jisou_image_verification_failed",
    "jisou_image_verification_required",
})
RECEIPT_CONTRACT_VERSION = "search-join-mutation-boundary-v1"


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
        rows = _load_rows(session, task_id, assignment_ids)
        changed = 0
        for row in rows:
            before = _row_output(row)
            _rebase_closed_unknown(row)
            _record_pre_accept_receipt(row)
            _finalize_fact_first_dispatch(session, row["action"])
            after = _row_output(_load_row(session, task_id, row["assignment"]))
            changed += before["assignment_state"] != after["assignment_state"]
            session.add(AuditLog(
                tenant_id=row["action"].tenant_id,
                actor=actor[:100],
                action="搜索点击验证码前置未执行收口",
                target_type="search_click_assignment",
                target_id=row["assignment"].id,
                detail=json.dumps({
                    "approval_ref": approval_ref,
                    "receipt_contract_version": RECEIPT_CONTRACT_VERSION,
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


def _load_rows(session, task_id: str, assignment_ids: tuple[str, ...]) -> list[dict]:
    ids = tuple(dict.fromkeys(value.strip() for value in assignment_ids if value.strip()))
    if not ids:
        raise ValueError("search_click_pre_accept_assignment_ids_required")
    assignments = list(session.scalars(select(SearchClickAssignment).where(
        SearchClickAssignment.task_id == task_id,
        SearchClickAssignment.id.in_(ids),
    )))
    if len(assignments) != len(ids):
        raise ValueError("search_click_pre_accept_assignment_set_mismatch")
    rows = [_load_row(session, task_id, assignment) for assignment in assignments]
    return sorted(rows, key=lambda row: row["assignment"].id)


def _load_row(session, task_id: str, assignment: SearchClickAssignment) -> dict:
    action = session.get(Action, assignment.action_id)
    if action is None:
        raise ValueError("search_click_pre_accept_action_missing")
    attempt = session.scalar(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id == action.id,
    ).order_by(ExecutionAttempt.attempt_no.desc()).limit(1))
    obligation = session.get(SearchClickFulfillmentObligation, assignment.obligation_id)
    journal = session.scalar(select(GatewayRequestEvidenceJournal).where(
        GatewayRequestEvidenceJournal.action_id == action.id,
        GatewayRequestEvidenceJournal.execution_attempt_id == (attempt.id if attempt else ""),
    ).limit(1))
    safe_fact = session.scalar(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.action_id == action.id,
        FulfillmentRemoteFact.fact_kind == SAFE_NOT_EXECUTED_FACT,
    ).order_by(FulfillmentRemoteFact.observed_at.desc()).limit(1))
    closed_fact = session.scalar(select(FulfillmentRemoteFact).where(
        FulfillmentRemoteFact.action_id == action.id,
        FulfillmentRemoteFact.fact_kind == "unknown_deadline_closed",
    ).order_by(FulfillmentRemoteFact.observed_at.desc()).limit(1))
    _validate_row(
        task_id,
        assignment,
        action,
        obligation,
        attempt,
        journal,
        safe_fact,
        closed_fact,
    )
    return {
        "assignment": assignment,
        "action": action,
        "obligation": obligation,
        "attempt": attempt,
        "journal": journal,
        "fact": safe_fact,
        "closed_fact": closed_fact,
    }


def _validate_row(
    task_id,
    assignment,
    action,
    obligation,
    attempt,
    journal,
    safe_fact,
    closed_fact,
) -> None:
    if action.task_id != task_id or action.task_type != "search_click":
        raise ValueError("search_click_pre_accept_task_binding_invalid")
    if action.action_type not in {"search_join", "search_join_membership"}:
        raise ValueError("search_click_pre_accept_action_type_invalid")
    if assignment.state == SAFE_NOT_EXECUTED_FACT:
        if safe_fact is None:
            raise ValueError("search_click_pre_accept_safe_fact_missing")
        return
    if assignment.state not in {"gateway_unknown", "closed_unknown"}:
        raise ValueError("search_click_pre_accept_assignment_state_invalid")
    if obligation is None or obligation.id != assignment.obligation_id:
        raise ValueError("search_click_pre_accept_obligation_binding_invalid")
    payload = dict(action.payload or {})
    if payload.get("search_click_assignment_id") != assignment.id:
        raise ValueError("search_click_pre_accept_action_assignment_invalid")
    if payload.get("search_click_obligation_id") != obligation.id:
        raise ValueError("search_click_pre_accept_action_obligation_invalid")
    if obligation.source_action_id != action.id:
        raise ValueError("search_click_pre_accept_source_action_invalid")
    if attempt is None or journal is None or attempt.gateway_call_started_at is None:
        raise ValueError("search_click_pre_accept_evidence_missing")
    if assignment.state == "closed_unknown" and (
        action.status != "closed_unknown"
        or obligation.status != "closed_unknown"
        or closed_fact is None
        or closed_fact.action_id != action.id
        or closed_fact.attempt_id != attempt.id
    ):
        raise ValueError("search_click_pre_accept_closed_unknown_fact_invalid")
    if journal.state != "recorded" or journal.remote_mutation_state != "unknown":
        raise ValueError("search_click_pre_accept_journal_state_invalid")
    if journal.remote_message_id or journal.remote_fact_id or attempt.remote_message_id:
        raise ValueError("search_click_pre_accept_remote_identity_present")
    result = dict(action.result or {})
    _validate_pre_accept_result(result)
    if result.get("callback_mutation_started") is True or result.get("target_click_observed") is True:
        raise ValueError("search_click_pre_accept_callback_or_click_present")


def _validate_pre_accept_result(result: dict) -> None:
    code = str(result.get("error_code") or "")
    if code not in PRE_ACCEPT_ERROR_CODES:
        raise ValueError("search_click_pre_accept_error_code_invalid")
    if code == "jisou_image_verification_required":
        if str(result.get("image_verification_reason") or "") not in PRE_ACCEPT_REASONS:
            raise ValueError("search_click_pre_accept_reason_invalid")
        return
    expected = {
        "bot_human_verification_required": ("page_classified", "verification_page"),
        "jisou_image_verification_failed": (
            "image_verification_failed",
            "verification_image_page",
        ),
    }[code]
    actual = (
        str(result.get("protocol_event_type") or ""),
        str(result.get("jisou_page_phase") or ""),
    )
    if actual != expected:
        raise ValueError("search_click_pre_accept_protocol_fact_invalid")


def _rebase_closed_unknown(row: dict) -> None:
    assignment = row["assignment"]
    if assignment.state != "closed_unknown":
        return
    assignment.state = "gateway_unknown"
    assignment.version = int(assignment.version or 1) + 1
    row["obligation"].status = "unknown_after_send"


def _record_pre_accept_receipt(row: dict) -> None:
    action = row["action"]
    attempt = row["attempt"]
    receipt = {
        "source": "search_join_adapter",
        "contract_version": RECEIPT_CONTRACT_VERSION,
        "reason": str(
            (action.result or {}).get("image_verification_reason")
            or (action.result or {}).get("error_code")
            or ""
        ),
        "remote_mutation_started": False,
        "recorded_at": _now().isoformat(),
    }
    action.result = {
        **dict(action.result or {}),
        "remote_mutation_started": False,
        "pre_accept_rejection": receipt,
    }
    attempt.result_snapshot = {
        **dict(attempt.result_snapshot or {}),
        "remote_mutation_started": False,
        "pre_accept_rejection": receipt,
    }
    action.status = "failed"
    action.unknown_deadline_at = None
    attempt.status = "failed"


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
        "fact_id": row["fact"].fact_id if row["fact"] else "",
        "closed_fact_id": (
            row["closed_fact"].fact_id if row["closed_fact"] else ""
        ),
        "journal_id": row["journal"].id if row["journal"] else "",
    }


def _require_approval(actor: str, approval_ref: str) -> None:
    if not actor.strip() or not approval_ref.strip():
        raise ValueError("search_click_pre_accept_actor_and_approval_required")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile typed pre-accept search verification failures.")
    parser.add_argument("command", choices=("preview", "apply"))
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--assignment-id", action="append", required=True)
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-ref", default="")
    args = parser.parse_args(argv)
    ids = tuple(args.assignment_id)
    result = preview(args.task_id, ids) if args.command == "preview" else apply_reconciliation(
        args.task_id,
        ids,
        actor=args.actor,
        approval_ref=args.approval_ref,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
