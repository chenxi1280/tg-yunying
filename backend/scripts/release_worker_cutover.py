"""Prepare and complete one all-worker release; never replay failed installation."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
import time

from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

from app.config import get_settings
from app.database import SessionLocal
from app.models import DispatchClaimScope
from app.services.task_center.release_cutover import (
    ReleaseIdentity, load_cutover_plan, prepare_cutover, record_verified_cutover,
    verify_reused_batch,
)
from app.services.task_center.release_cutover_fingerprint import contract_source_fingerprint
from scripts.manage_shared_dispatch_contract import run_command
from scripts.takeover_ai_content_scope import run_apply, run_preview
from scripts.takeover_all_task_fulfillment import run_takeover

BACKEND_ROOT = Path(__file__).resolve().parents[1]
TAKEOVER_BATCH_SIZE = 100


def verify_schema(session) -> None:
    expected = set(ScriptDirectory(str(BACKEND_ROOT / "migrations")).get_heads())
    current = set(MigrationContext.configure(session.connection()).get_current_heads())
    if current != expected:
        raise ValueError(f"release_schema_revision_mismatch:expected={sorted(expected)},current={sorted(current)}")


def prepare(identity: ReleaseIdentity, stopped_before: str) -> dict:
    run_command(
        "retire-stopped-writers", actor=identity.actor,
        approval_ref=identity.approval_ref, stopped_before=stopped_before,
    )
    with SessionLocal() as session:
        verify_schema(session)
        plan = prepare_cutover(session, get_settings(), identity)
        session.commit()
    return plan


def complete(identity: ReleaseIdentity, plan_id: int) -> dict:
    with SessionLocal() as session:
        verify_schema(session)
        plan = load_cutover_plan(session, plan_id, identity)
        scope = session.get(DispatchClaimScope, plan["scope_id"])
        if scope is None or scope.contract_activation_state != "preparing":
            raise ValueError("release_plan_not_preparing")
        if plan["mode"] == "ordinary":
            verify_reused_batch(session, get_settings(), plan)
    run_command("verify-ready")
    recovered = run_command(
        "reconcile-ledger", actor=identity.actor, approval_ref=identity.approval_ref,
    )
    batch_id = plan["takeover_head_batch_id"]
    if plan["mode"] == "upgrade":
        batch_id = upgrade_takeover(identity)
    run_command(
        "activate", actor=identity.actor, approval_ref=identity.approval_ref,
        takeover_head_batch_id=batch_id,
    )
    verified = run_command("verify-active")
    with SessionLocal() as session:
        # Recheck the exact plan after potentially long takeover work.
        plan = load_cutover_plan(session, plan_id, identity)
        result = record_verified_cutover(session, plan, batch_id)
        session.commit()
    return {**result, "recovery": recovered, "verification": verified}


def upgrade_takeover(identity: ReleaseIdentity) -> str:
    for apply in (False, True):
        result = run_takeover(apply=apply, tenant_id=None)
        print(json.dumps({"phase": "fulfillment_takeover", "apply": apply,
            "scanned": result["scanned"], "changed": result["changed"],
            "blocker_count": len(result["blockers"]), "failure_count": len(result["failures"])}), file=sys.stderr)
        if result["failures"]:
            raise ValueError(f"release_fulfillment_takeover_failed:{result['failures']}")
    batch = run_preview(
        actor=identity.actor, approval_ref=identity.approval_ref,
        release_version=identity.sha,
        config_version=get_settings().dispatch_rebuild_contract_version,
    )
    result = run_apply(
        batch_id=batch["batch_id"], classification_hash=batch["classification_hash"],
        expected_counts=batch["classification_counts"], actor=identity.actor,
        approval_ref=identity.approval_ref, batch_size=TAKEOVER_BATCH_SIZE,
    )
    if result["status"] != "completed":
        raise ValueError(f"release_content_takeover_incomplete:{result['status']}")
    return batch["batch_id"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "complete"))
    parser.add_argument("--stopped-before", default="")
    parser.add_argument("--plan-id", type=int)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--approval-ref", required=True)
    args = parser.parse_args()
    identity = ReleaseIdentity(
        sha=os.environ["RELEASE_SHA"],
        source_fingerprint=contract_source_fingerprint(BACKEND_ROOT),
        actor=args.actor, approval_ref=args.approval_ref,
    )
    started = time.monotonic()
    if args.operation == "prepare":
        if not args.stopped_before:
            parser.error("prepare requires --stopped-before")
        result = prepare(identity, args.stopped_before)
    else:
        if args.plan_id is None:
            parser.error("complete requires --plan-id")
        result = complete(identity, args.plan_id)
    print(json.dumps({**result, "elapsed_seconds": round(time.monotonic() - started, 3)}, default=str))


if __name__ == "__main__":
    main()
