from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AccountPool, Action, AuditLog, ExecutionAttempt, Task, Tenant, TgAccount
from app.services.task_center.engagement_legacy_occupancy import (
    LegacyOccupancyScope, read_legacy_attempt_occupancy,
)
from app.services.task_center.telegram_worker_exit_evidence import validate_exit_proofs
from app.services.task_center.telegram_worker_exit_reconcile import (
    ACK_AUDIT, WorkerExitOperation, apply_worker_exits, preview_worker_exits, verify_worker_exits,
)
from tests.test_engagement_runtime_resources import _session


pytestmark = pytest.mark.no_postgres
CONTAINER = "ab" * 32
SHA = "c" * 40
CALL_AT = datetime(2026, 9, 5, 9)
OBSERVED_AT = datetime(2026, 9, 5, 12)
OPERATION = WorkerExitOperation("QA", "original-call-exit-qa", SHA)


def _event(message):
    return {"message": message, "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
        "source_host": "qa-host", "source_ref": "journal:docker.service:original-event"}


def _evidence():
    delete = (f'time="2026-09-05T02:00:01+00:00" level=info msg="ignoring event" '
        f'container={CONTAINER} module=libcontainerd namespace=moby '
        'topic=/tasks/delete type="*events.TaskDelete"')
    exited = ('time="2026-09-05T02:00:02+00:00" level=warning '
        f'msg="ShouldRestart failed, container will not be restarted" container={CONTAINER} '
        'exitStatus="{137 2026-09-05 02:00:00.000000001 +0000 UTC}" hasBeenManuallyStopped=true')
    return {"schema_version": 1, "source_host": "qa-host",
        "executor_contract": "docker_pid1_local_telethon_v1", "collected_at": "2026-09-05T03:00:00+00:00",
        "exits": [{"container_id": CONTAINER, "delete_event": _event(delete), "exit_event": _event(exited)}]}


def _legacy(session, *, when=CALL_AT, state="result_unknown"):
    session.add(Tenant(id=1, name="QA exits"))
    session.flush()
    session.add(AccountPool(id=1, tenant_id=1, name="QA pool"))
    session.flush()
    session.add(TgAccount(id=11, tenant_id=1, pool_id=1, display_name="QA account", phone_masked="QA"))
    task = Task(tenant_id=1, type="channel_like", name="original task", status="stopped")
    session.add(task)
    session.flush()
    action = Action(tenant_id=1, task_id=task.id, task_type=task.type, action_type="like_message",
        account_id=11, status="unknown_after_send", task_lifecycle_epoch=1, pacing_due_at=when,
        payload={"original_source": "retained"}, result={"outcome": "unknown"})
    session.add(action)
    session.flush()
    attempt = ExecutionAttempt(tenant_id=1, action_id=action.id, account_id=11,
        worker_id=f"{CONTAINER[:12]}:1", task_lifecycle_epoch=1, status=state,
        before_call_at=when - timedelta(seconds=1), gateway_call_started_at=when,
        after_call_at=when + timedelta(seconds=1), failure_type="original_timeout",
        result_snapshot={"remote_mutation_started": None, "transport_termination_state": "unproven"})
    session.add(attempt)
    session.flush()
    return action, attempt


def _spec(attempt):
    return {"tenant_id": 1, "deployed_sha": SHA, "attempt_ids": [attempt.id],
        "expected_attempt_count": 1, "evidence": _evidence()}


def test_exit_ack_preserves_unknown_dates_and_current_day_budget():
    with _session() as session:
        action, attempt = _legacy(session)
        session.commit()
        original = (action.status, action.payload, action.result, action.pacing_due_at,
            attempt.status, attempt.gateway_call_started_at, attempt.after_call_at, attempt.failure_type)
        today = LegacyOccupancyScope(1, (11,), date(2026, 9, 6))
        assert read_legacy_attempt_occupancy(session, today)[0].remote_inflight
        preview = json.loads(json.dumps(preview_worker_exits(session, _spec(attempt))))
        receipt = apply_worker_exits(session, preview, OPERATION)
        session.commit()
        assert original == (action.status, action.payload, action.result, action.pacing_due_at,
            attempt.status, attempt.gateway_call_started_at, attempt.after_call_at, attempt.failure_type)
        assert read_legacy_attempt_occupancy(session, today) == ()
        row, = read_legacy_attempt_occupancy(session, LegacyOccupancyScope(1, (11,), date(2026, 9, 5)))
        assert not row.remote_inflight and row.attempt_state == "result_unknown"
        assert row.original_task_day == date(2026, 9, 5)
        with Session(session.get_bind()) as verification:
            assert verify_worker_exits(verification, receipt)["business_fields_preserved"]
        assert apply_worker_exits(session, preview, OPERATION) == receipt
        session.commit()
        assert session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == ACK_AUDIT)) == 1


def test_original_missing_day_and_request_are_not_filled():
    with _session() as session:
        action, attempt = _legacy(session)
        action.pacing_due_at = None
        attempt.after_call_at = None
        attempt.status = "gateway_call_started"
        session.commit()
        receipt = apply_worker_exits(session, preview_worker_exits(session, _spec(attempt)), OPERATION)
        session.commit()
        assert action.pacing_due_at is None and attempt.after_call_at is None
        assert "gateway_request_identity" not in attempt.result_snapshot
        assert attempt.status == "gateway_call_started"
        row, = read_legacy_attempt_occupancy(session, LegacyOccupancyScope(1, (11,), date(2026, 9, 5)))
        assert not row.remote_inflight and row.issues == ("original_task_day_unproven",)
        assert verify_worker_exits(session, receipt)["acknowledged"] == 1


@pytest.mark.parametrize("field,value", [
    ("worker_id", "someone:1"), ("worker_id", f"{CONTAINER[:12]}:2"),
    ("status", "success"), ("gateway_call_started_at", None),
    ("gateway_call_started_at", CALL_AT + timedelta(hours=2)), ("account_id", 12),
    ("task_lifecycle_epoch", 2), ("remote_message_id", "123"),
])
def test_original_call_mismatch_or_unissued_never_receives_ack(field, value):
    with _session() as session:
        _, attempt = _legacy(session)
        setattr(attempt, field, value)
        session.flush()
        with pytest.raises(ValueError, match="worker_exit_"):
            preview_worker_exits(session, _spec(attempt))
        assert attempt.result_snapshot["transport_termination_state"] == "unproven"


@pytest.mark.parametrize("change", ["hash", "container", "host", "exit_time", "event_kind", "no_timezone"])
def test_host_events_must_prove_the_exact_original_exit(change):
    evidence = _evidence()
    record = evidence["exits"][0]
    event = record["exit_event"]
    if change == "hash":
        event["message_sha256"] = "0" * 64
    elif change == "container":
        record["container_id"] = "d" * 64
    elif change == "host":
        event["source_host"] = "another-host"
    else:
        replacements = {"exit_time": ("2026-09-05 02:00:00", "2026-09-05 03:00:00"),
            "event_kind": ("ShouldRestart failed, container will not be restarted", "stop requested"),
            "no_timezone": ("2026-09-05T02:00:02+00:00", "2026-09-05T02:00:02")}
        event.update(_event(event["message"].replace(*replacements[change])))
    with pytest.raises(ValueError, match="worker_exit_"):
        validate_exit_proofs(evidence, observed_at=OBSERVED_AT)


def test_two_full_container_ids_cannot_claim_one_worker_prefix():
    evidence = _evidence()
    other = deepcopy(evidence["exits"][0])
    replacement = CONTAINER[:12] + "c" * 52
    other["container_id"] = replacement
    for key in ("delete_event", "exit_event"):
        other[key] = _event(other[key]["message"].replace(CONTAINER, replacement))
    evidence["exits"].append(other)
    with pytest.raises(ValueError, match="prefix_ambiguous"):
        validate_exit_proofs(evidence, observed_at=OBSERVED_AT)


def test_preview_drift_and_rollback_preserve_original_snapshot():
    with _session() as session:
        action, attempt = _legacy(session)
        session.commit()
        preview = preview_worker_exits(session, _spec(attempt))
        action.payload = {"original_source": "changed"}
        session.commit()
        with pytest.raises(ValueError, match="preview_conflict"):
            apply_worker_exits(session, preview, OPERATION)
        session.rollback()
        fresh = preview_worker_exits(session, _spec(attempt))
        apply_worker_exits(session, fresh, OPERATION)
        session.rollback()
        assert attempt.result_snapshot["transport_termination_state"] == "unproven"
        assert session.scalar(select(func.count()).select_from(AuditLog)) == 0


def test_tampered_receipt_does_not_pass_independent_readback():
    with _session() as session:
        _, attempt = _legacy(session)
        receipt = apply_worker_exits(session, preview_worker_exits(session, _spec(attempt)), OPERATION)
        session.commit()
        tampered = deepcopy(receipt)
        tampered["attempts"][0]["business_hash"] = "0" * 64
        with pytest.raises(ValueError, match="receipt_not_audited"):
            verify_worker_exits(session, tampered)


@pytest.mark.parametrize("change", ["count", "duplicate", "missing", "tenant", "sha"])
def test_exact_scope_and_release_are_required(change):
    with _session() as session:
        _, attempt = _legacy(session)
        spec = _spec(attempt)
        if change == "duplicate":
            spec["attempt_ids"].append(attempt.id)
        else:
            field, value = {"count": ("expected_attempt_count", 2), "missing": ("attempt_ids", ["absent"]),
                "tenant": ("tenant_id", 2), "sha": ("deployed_sha", "invalid")}[change]
            spec[field] = value
        with pytest.raises(ValueError, match="worker_exit_"):
            preview_worker_exits(session, spec)


def test_existing_unified_resources_keep_their_formal_recovery_path():
    from app.models import AccountBehaviorBudgetReservation, AccountPoolConcurrencyLease
    from tests.test_telegram_termination import _unknown

    with _session() as session:
        _, attempt = _unknown(session)
        budget = session.scalar(select(AccountBehaviorBudgetReservation))
        lease = session.scalar(select(AccountPoolConcurrencyLease))
        before = budget.state, lease.state, dict(attempt.result_snapshot)
        with pytest.raises(ValueError, match="requires_legacy_resource_path"):
            preview_worker_exits(session, _spec(attempt))
        assert before == (budget.state, lease.state, attempt.result_snapshot)


def test_missing_positive_exit_evidence_is_not_an_absence_certificate():
    evidence = _evidence()
    evidence["exits"] = []
    evidence["container_matches"] = []
    evidence["process_matches"] = []
    with pytest.raises(ValueError, match="positive_evidence_required"):
        validate_exit_proofs(evidence, observed_at=OBSERVED_AT)


def test_future_collection_cannot_prove_a_completed_exit():
    evidence = _evidence()
    evidence["collected_at"] = "2026-09-06T00:00:00+00:00"
    with pytest.raises(ValueError, match="collection_in_future"):
        validate_exit_proofs(evidence, observed_at=OBSERVED_AT)
