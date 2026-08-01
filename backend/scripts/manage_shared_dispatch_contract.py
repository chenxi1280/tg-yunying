from __future__ import annotations

import argparse
from datetime import datetime
import json

from app.config import get_settings
from app.database import SessionLocal
from app.models import AuditLog, DispatchClaimScope
from app.services.task_center.dispatch_runtime_control import (
    activate_dispatch_runtime_contract,
    dispatch_runtime_candidate_status,
    stage_dispatch_runtime_contract,
    retire_stopped_dispatch_writers,
    verify_dispatch_runtime_active,
    verify_dispatch_runtime_candidate,
)
from app.services.task_center.dispatch_activation_ledger import (
    reconcile_dispatch_ledgers_for_activation,
    recover_fenced_dispatch_actions,
)


def run_command(
    command: str,
    *,
    actor: str = "",
    approval_ref: str = "",
    takeover_head_batch_id: str = "",
    stopped_before: str = "",
) -> dict:
    settings = get_settings()
    with SessionLocal() as session:
        if command == "status":
            return dispatch_runtime_candidate_status(session, settings)
        if command == "verify-ready":
            return verify_dispatch_runtime_candidate(session, settings)
        if command == "verify-active":
            return verify_dispatch_runtime_active(session, settings)
        _require_approval(actor, approval_ref)
        if command == "retire-stopped-writers":
            return _retire_stopped_writers(
                session,
                actor=actor,
                approval_ref=approval_ref,
                stopped_before=stopped_before,
            )
        if command == "reconcile-ledger":
            return _reconcile_activation_ledger(
                session,
                settings,
                actor=actor,
                approval_ref=approval_ref,
            )
        return _mutate_runtime_contract(
            session,
            settings,
            command=command,
            actor=actor,
            approval_ref=approval_ref,
            takeover_head_batch_id=takeover_head_batch_id,
        )


def _mutate_runtime_contract(
    session,
    settings,
    *,
    command: str,
    actor: str,
    approval_ref: str,
    takeover_head_batch_id: str,
) -> dict:
    if command == "stage":
        scope = stage_dispatch_runtime_contract(session, settings)
    elif command == "activate":
        scope = activate_dispatch_runtime_contract(
            session,
            settings,
            takeover_head_batch_id=takeover_head_batch_id,
        )
    else:
        raise ValueError("shared_dispatch_command_invalid")
    _write_audit(
        session,
        scope_id=scope.id,
        actor=actor,
        approval_ref=approval_ref,
        action=command,
        takeover_head_batch_id=takeover_head_batch_id,
    )
    session.commit()
    return dispatch_runtime_candidate_status(session, settings)


def _reconcile_activation_ledger(
    session,
    settings,
    *,
    actor: str,
    approval_ref: str,
) -> dict:
    recovered = 0
    while True:
        changed = recover_fenced_dispatch_actions(
            session,
            actor=actor,
            limit=100,
        )
        session.commit()
        recovered += changed
        if changed == 0:
            break
    result = reconcile_dispatch_ledgers_for_activation(session, settings)
    scope = session.get(DispatchClaimScope, result["scope_id"])
    if scope is None:
        raise RuntimeError("dispatch_scope_missing_after_reconcile")
    _write_audit(
        session,
        scope_id=scope.id,
        actor=actor,
        approval_ref=approval_ref,
        action="reconcile-ledger",
        takeover_head_batch_id="",
    )
    session.commit()
    return {**result, "recovered_fenced_action_count": recovered}


def _require_approval(actor: str, approval_ref: str) -> None:
    if not actor.strip() or not approval_ref.strip():
        raise ValueError("release_actor_and_approval_required")


def _retire_stopped_writers(
    session,
    *,
    actor: str,
    approval_ref: str,
    stopped_before: str,
) -> dict:
    cutoff = _parse_stopped_before(stopped_before)
    retired_ids = retire_stopped_dispatch_writers(
        session,
        stopped_before=cutoff,
        actor=actor,
    )
    session.add(AuditLog(
        tenant_id=None,
        actor=actor[:100],
        action="共享调度旧writer退役",
        target_type="worker_heartbeat",
        target_id=cutoff.isoformat(),
        detail=json.dumps({
            "approval_ref": approval_ref,
            "retired_worker_ids": retired_ids,
            "stopped_before": cutoff.isoformat(),
        }, ensure_ascii=False, sort_keys=True),
    ))
    session.commit()
    return {
        "retired_worker_count": len(retired_ids),
        "retired_worker_ids": retired_ids,
        "stopped_before": cutoff.isoformat(),
    }


def _parse_stopped_before(value: str) -> datetime:
    if not value.strip():
        raise ValueError("stopped_before_required")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _write_audit(
    session,
    *,
    scope_id: str,
    actor: str,
    approval_ref: str,
    action: str,
    takeover_head_batch_id: str,
) -> None:
    session.add(AuditLog(
        tenant_id=None,
        actor=actor[:100],
        action=f"共享调度合同{action}",
        target_type="dispatch_claim_scope",
        target_id=scope_id,
        detail=json.dumps({
            "approval_ref": approval_ref,
            "action": action,
            "takeover_head_batch_id": takeover_head_batch_id,
        }, ensure_ascii=False, sort_keys=True),
    ))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage shared dispatch contract.")
    parser.add_argument(
        "command", choices=(
            "status", "stage", "verify-ready", "reconcile-ledger", "activate",
            "verify-active", "retire-stopped-writers",
        ),
    )
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--takeover-head-batch-id", default="")
    parser.add_argument("--stopped-before", default="")
    args = parser.parse_args()
    result = run_command(
        args.command,
        actor=args.actor,
        approval_ref=args.approval_ref,
        takeover_head_batch_id=args.takeover_head_batch_id,
        stopped_before=args.stopped_before,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
