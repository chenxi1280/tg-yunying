from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.models import Action, AuditLog, RemoteReconcileCase
from app.services._common import gateway
from app.services.developer_apps import credentials_for_account
from app.services.task_center.dispatcher import project_dispatch_action_stats
from app.services.task_center.remote_reconciliation import (
    apply_remote_reconcile_evidence,
    evidence_from_gateway_journal,
)
from app.services.task_center.remote_history_evidence import (
    preview_remote_history_evidence,
)


def preview_case(case_id: str, *, evidence_source: str = "journal") -> dict:
    with SessionLocal() as session:
        evidence = _case_evidence(session, case_id, evidence_source)
        case = session.get(RemoteReconcileCase, case_id)
        return {
            "case_id": case_id,
            "case_state": case.state if case else "missing",
            "proposed_result": evidence.result,
            "source": evidence.source,
            "evidence_fingerprint": evidence.evidence_fingerprint,
            "remote_message_id": evidence.remote_message_id,
            "remote_fact_id": evidence.remote_fact_id,
            "failure_code": evidence.failure_code,
        }


def apply_case(
    case_id: str,
    *,
    expected_evidence_fingerprint: str,
    actor: str,
    approval_ref: str,
    evidence_source: str = "journal",
) -> dict:
    _require_approval(actor, approval_ref)
    with SessionLocal() as session:
        evidence = _case_evidence(session, case_id, evidence_source)
        if evidence.evidence_fingerprint != expected_evidence_fingerprint:
            raise ValueError("remote_evidence_fingerprint_mismatch")
        outcome = apply_remote_reconcile_evidence(
            session,
            case_id,
            evidence,
            actor=actor,
        )
        case = session.get(RemoteReconcileCase, case_id)
        _write_approval_audit(session, case, actor, approval_ref, outcome.state)
        action_id = case.action_id
        session.commit()
    _project_stats(action_id)
    return {
        "case_id": outcome.case_id,
        "state": outcome.state,
        "changed": outcome.changed,
        "evidence_hash": outcome.evidence_hash,
    }


def _case_evidence(session, case_id: str, evidence_source: str):
    if evidence_source == "journal":
        return evidence_from_gateway_journal(session, case_id)
    if evidence_source == "telegram-history":
        return preview_remote_history_evidence(
            session,
            case_id,
            gateway_client=gateway,
            credentials_resolver=credentials_for_account,
        )
    raise ValueError("remote_evidence_source_invalid")


def _project_stats(action_id: str) -> None:
    with SessionLocal() as session:
        action = session.get(Action, action_id)
        if action is None:
            return
        project_dispatch_action_stats(session, action)
        session.commit()


def _write_approval_audit(
    session,
    case: RemoteReconcileCase,
    actor: str,
    approval_ref: str,
    result: str,
) -> None:
    session.add(AuditLog(
        tenant_id=None,
        actor=actor[:100],
        action="远端发送结果核验审批",
        target_type="remote_reconcile_case",
        target_id=case.id,
        detail=json.dumps({
            "approval_ref": approval_ref,
            "result": result,
        }, ensure_ascii=False, sort_keys=True),
    ))


def _require_approval(actor: str, approval_ref: str) -> None:
    if not actor.strip() or not approval_ref.strip():
        raise ValueError("remote_reconcile_actor_and_approval_required")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile Gateway result journal.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preview = subparsers.add_parser("preview")
    preview.add_argument("--case-id", required=True)
    preview.add_argument(
        "--evidence-source",
        choices=("journal", "telegram-history"),
        default="journal",
    )
    apply = subparsers.add_parser("apply")
    apply.add_argument("--case-id", required=True)
    apply.add_argument("--expected-evidence-fingerprint", required=True)
    apply.add_argument("--actor", required=True)
    apply.add_argument("--approval-ref", required=True)
    apply.add_argument(
        "--evidence-source",
        choices=("journal", "telegram-history"),
        default="journal",
    )
    args = parser.parse_args()
    result = (
        preview_case(args.case_id, evidence_source=args.evidence_source)
        if args.command == "preview"
        else apply_case(
            args.case_id,
            expected_evidence_fingerprint=args.expected_evidence_fingerprint,
            actor=args.actor,
            approval_ref=args.approval_ref,
            evidence_source=args.evidence_source,
        )
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
