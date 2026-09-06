"""Batch-optimized cleanup for retired engagement tasks cutover."""
import json
import os
import sys
import time
from sqlalchemy import text
from app.database import SessionLocal
from app.models import Action
from app.services.task_center.engagement_direct_cutover import (
    CutoverOperation,
    _require_operation,
    _require_receipt_audit,
    verify_retirement,
)
from app.services.task_center.engagement_retirement_cleanup import (
    _retire_action,
    _uncalled_actions,
    cleanup_remaining,
    _audit_stage,
    CLEANUP_AUDIT,
)


def main():
    receipt_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/unified-cutover-receipt.json"
    with open(receipt_path) as f:
        receipt = json.load(f)

    deployed_sha = os.getenv("RELEASE_SHA", "")
    expected_sha = sys.argv[2] if len(sys.argv) > 2 else "0b646f05703378f80bcedd03ed079cd0de839a42"
    if deployed_sha != expected_sha:
        raise ValueError(f"RELEASE_SHA mismatch: {deployed_sha} != {expected_sha}")

    actor = "codex"
    audit_ref = "cutover-cleanup-v1"
    operation = CutoverOperation(actor, audit_ref, deployed_sha)

    ids = list(receipt["mapping"].keys())
    print(f"Starting batch cleanup for {len(ids)} retired tasks...")

    with SessionLocal() as session:
        _require_operation(operation)
        _require_receipt_audit(session, receipt)
        verify_retirement(session, receipt)

    # Step 1: Release unbound account pacing reservations
    t0 = time.time()
    with SessionLocal() as session:
        res = session.execute(text("""
            UPDATE account_pacing_reservations
            SET state = 'released',
                version = COALESCE(version, 1) + 1
            WHERE task_id = ANY(:ids) AND state = 'reserved' AND action_id IS NULL
        """), {"ids": ids})
        session.commit()
        unbound_res_count = res.rowcount
    print(f"Step 1: Released {unbound_res_count} unbound reservations in {time.time()-t0:.2f}s")

    # Step 2: Release unbound view identities
    t0 = time.time()
    with SessionLocal() as session:
        res = session.execute(text("""
            UPDATE channel_view_daily_identity_owners
            SET state = 'available',
                obligation_id = NULL,
                request_identity = 'released:' || id || ':' || (version + 1),
                version = version + 1
            WHERE logical_task_id = ANY(:ids) AND state = 'pre_gateway' AND action_id IS NULL
        """), {"ids": ids})
        session.commit()
        unbound_view_count = res.rowcount
    print(f"Step 2: Released {unbound_view_count} unbound view identities in {time.time()-t0:.2f}s")

    # Step 3: Release pacing reservations on already terminal actions
    t0 = time.time()
    with SessionLocal() as session:
        res = session.execute(text("""
            UPDATE account_pacing_reservations
            SET state = 'released',
                version = COALESCE(version, 1) + 1
            WHERE task_id = ANY(:ids) 
              AND state IN ('reserved', 'bound')
              AND action_id IN (
                  SELECT id FROM actions 
                  WHERE task_id = ANY(:ids) AND status IN ('skipped', 'failed', 'cancelled')
              )
        """), {"ids": ids})
        session.commit()
        terminal_pacing_count = res.rowcount
    print(f"Step 3: Released {terminal_pacing_count} lingering pacing reservations on terminal actions in {time.time()-t0:.2f}s")

    # Step 4: Invalidate plan slots for open generation jobs
    t0 = time.time()
    with SessionLocal() as session:
        res = session.execute(text("""
            UPDATE ai_content_window_plan_slots
            SET state = 'invalidated',
                claimed_by_job_id = NULL,
                lease_expires_at = NULL,
                version = COALESCE(version, 1) + 1
            WHERE id IN (
                SELECT window_slot_id FROM generation_jobs 
                WHERE task_id = ANY(:ids) AND window_slot_id IS NOT NULL
            ) AND state IN ('claimed', 'candidate_ready')
        """), {"ids": ids})
        session.commit()
        slots_count = res.rowcount
    print(f"Step 4: Invalidated {slots_count} window plan slots in {time.time()-t0:.2f}s")

    # Step 5: Retire open generation jobs in chunks of 50000
    t0 = time.time()
    total_jobs_retired = 0
    while True:
        with SessionLocal() as session:
            res = session.execute(text("""
                UPDATE generation_jobs
                SET state = 'cancelled',
                    generation_owner_id = '',
                    lease_expires_at = NULL,
                    next_retry_at = NULL,
                    job_version = COALESCE(job_version, 1) + 1,
                    generation_lease_epoch = COALESCE(generation_lease_epoch, 0) + 1,
                    evaluator_evidence = jsonb_set(COALESCE(evaluator_evidence::jsonb, '{}'::jsonb), '{invalidation_reason}', to_jsonb('task_retired'::text))::json
                WHERE id IN (
                    SELECT id FROM generation_jobs
                    WHERE task_id = ANY(:ids) 
                      AND state IN ('pending', 'generating', 'unknown', 'ready')
                      AND COALESCE(evaluator_evidence->>'invalidation_reason', '') != 'task_retired'
                    LIMIT 50000
                )
            """), {"ids": ids})
            session.commit()
            count = res.rowcount
            total_jobs_retired += count
            if count > 0:
                print(f"  Retired chunk of {count} jobs (total so far: {total_jobs_retired})...")
            if count == 0:
                break
    print(f"Step 5: Retired {total_jobs_retired} open generation jobs in {time.time()-t0:.2f}s")

    # Step 6: Retire open actions via domain _retire_action with _uncalled_actions filter
    t0 = time.time()
    total_actions_retired = 0
    while True:
        with SessionLocal() as session:
            actions = list(session.scalars(
                _uncalled_actions(ids).limit(1000).with_for_update(nowait=True).execution_options(populate_existing=True)
            ))
            if not actions:
                break
            for action in actions:
                _retire_action(session, action)
            session.commit()
            total_actions_retired += len(actions)
            print(f"  Retired batch of {len(actions)} uncalled actions (total: {total_actions_retired})...")
    print(f"Step 6: Retired {total_actions_retired} uncalled actions in {time.time()-t0:.2f}s")

    # Step 7: Verification and Audit logging
    t0 = time.time()
    with SessionLocal() as session:
        remaining = cleanup_remaining(session, receipt)
        print(f"Step 7: Verification cleanup_remaining result: {remaining} in {time.time()-t0:.2f}s")
        if any(remaining.values()):
            raise ValueError(f"engagement_cutover_cleanup_incomplete:{remaining}")

        cleanup_summary = {
            "actions": total_actions_retired,
            "jobs": total_jobs_retired,
            "reservations": unbound_res_count + terminal_pacing_count,
            "remaining": remaining,
        }
        _audit_stage(session, {**receipt, "cleanup": cleanup_summary}, operation, action=CLEANUP_AUDIT)
        session.commit()
        print("Successfully logged cutover cleanup audit!")

    print("\nALL CLEANUP TASKS COMPLETED SUCCESSFULLY WITH ZERO REMAINING ITEMS!")


if __name__ == "__main__":
    main()
