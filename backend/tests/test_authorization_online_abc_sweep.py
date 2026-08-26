from __future__ import annotations

from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import (
    AuditLog,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
)
from app.services._common import _now
from app.services.authorization_dr.online_abc import (
    UNKNOWN_OPERATION_STATUSES,
    start_next_online_abc_item,
    sync_online_abc_batch,
)
from app.services.authorization_dr.online_abc_exception_queue import (
    list_online_abc_exceptions,
)
from app.services.authorization_dr.online_abc_manifest import (
    apply_full_online_abc_batch,
    preview_full_online_abc_batch,
)
from app.services.authorization_dr.online_abc_sweep import (
    CHECKPOINT_ACTION,
    apply_online_abc_sweep_start,
    online_abc_sweep_status,
    preview_online_abc_sweep_start,
    run_online_abc_sweep_once,
)
from tests import test_authorization_online_abc as abc_tests
from tests import test_authorization_online_abc_release_interrupted as release_tests


pytestmark = pytest.mark.no_postgres
START_KEY = "abc-sweep:test:v1"


@pytest.fixture
def db_session():
    fixture = abc_tests.session.__wrapped__()
    session = next(fixture)
    try:
        yield session
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass


def test_sweep_start_quarantines_same_unknown_operation_and_preserves_a(db_session) -> None:
    batch_id, account_id, operation_id = _stopped_unknown(db_session)
    before_a = _a_snapshot(db_session, account_id)
    preview = _start_preview(db_session, batch_id)

    result = _start_apply(db_session, batch_id, preview["fingerprint"])

    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    item = release_tests._item(db_session, batch_id, account_id)
    queue = list_online_abc_exceptions(db_session, batch_id)
    assert preview["current_exception"]["classification"] == "deferred_reconcile"
    assert result["batch"]["status"] == "sweeping"
    assert item.status == item.outcome == "deferred_reconcile"
    assert operation.status == "deferred_reconcile"
    assert operation.remote_call_state == "unknown"
    assert operation.reconcile_status == "quarantined"
    assert _operation_count(db_session, UNKNOWN_OPERATION_STATUSES) == 0
    assert _a_snapshot(db_session, account_id) == before_a
    assert queue["unresolved_count"] == 1
    assert queue["items"][0]["operation_id"] == operation_id


def test_initial_sweep_start_binds_stopped_batch_to_current_release(db_session) -> None:
    batch_id, _, _ = _stopped_unknown(db_session)
    current_release = "b" * 40
    preview = preview_online_abc_sweep_start(
        db_session, batch_id, runtime_release_sha=current_release,
        idempotency_key=START_KEY, requested_by="requester",
        approved_by="approver", approval_ref="ABC-FULL",
    )

    result = apply_online_abc_sweep_start(
        db_session, batch_id, runtime_release_sha=current_release,
        idempotency_key=START_KEY, expected_fingerprint=preview["fingerprint"],
        requested_by="requester", approved_by="approver", approval_ref="ABC-FULL",
    )

    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    assert preview["execution_release_rebind"]["required"] is True
    assert batch.execution_release_sha == current_release
    assert result["batch"]["execution_release_sha"] == current_release
    assert result["batch"]["status"] == "sweeping"


def test_generic_issue_marks_a_drift_manual_without_changing_a(db_session) -> None:
    batch_id = _full_batch(db_session)
    command = start_next_online_abc_item(
        db_session, batch_id, actor="approver", approval_ref="ABC-FULL",
    )
    account = db_session.get(TgAccount, command["account_id"])
    account.connection_generation += 1
    item = release_tests._item(db_session, batch_id, command["account_id"])
    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item.status = "stopped"
    item.outcome = item.blocker_code = "online_abc_primary_drift"
    batch.status = "stopped"
    db_session.commit()
    before_a = _a_snapshot(db_session, account.id)

    preview = _start_preview(db_session, batch_id)
    result = _start_apply(db_session, batch_id, preview["fingerprint"])

    item = release_tests._item(db_session, batch_id, account.id)
    assert preview["current_exception"]["classification"] == "deferred_issue"
    assert item.status == item.outcome == "manual_required"
    assert item.blocker_code == "online_abc_primary_drift"
    assert result["sweep"]["manual_required_count"] == 1
    assert _a_snapshot(db_session, account.id) == before_a


def test_typed_terminal_is_deferred_issue_and_operation_is_not_rewritten(db_session) -> None:
    batch_id = _full_batch(db_session)
    command = start_next_online_abc_item(
        db_session, batch_id, actor="approver", approval_ref="ABC-FULL",
    )
    operation = abc_tests._add_operation(
        db_session, command["account_id"], command["b_idempotency_key"], "failed",
    )
    operation.remote_call_state = "started"
    operation.blocker_code = "verification_code_unreadable"
    operation.remote_effect_started_at = _now()
    db_session.commit()
    sync_online_abc_batch(db_session, batch_id, actor="approver", approval_ref="ABC-FULL")
    before_operation = (
        operation.status, operation.remote_call_state, operation.blocker_code,
        operation.operation_version,
    )

    preview = _start_preview(db_session, batch_id)
    result = _start_apply(db_session, batch_id, preview["fingerprint"])

    operation = db_session.get(TgAuthorizationDrOperation, operation.id)
    item = release_tests._item(db_session, batch_id, command["account_id"])
    assert preview["current_exception"]["classification"] == "deferred_issue"
    assert item.status == item.outcome == "manual_required"
    assert result["sweep"]["manual_required_count"] == 1
    assert (
        operation.status, operation.remote_call_state, operation.blocker_code,
        operation.operation_version,
    ) == before_operation


def test_completed_checkpoint_is_projected_without_remote_replay(db_session, monkeypatch) -> None:
    batch_id = _full_batch(db_session)
    command = start_next_online_abc_item(
        db_session, batch_id, actor="approver", approval_ref="ABC-FULL",
    )
    abc_tests._qualify_primary(db_session, command["account_id"])
    abc_tests._add_operations(db_session, command, status="succeeded")
    db_session.get(TgAuthorizationOnlineAbcBatch, batch_id).status = "stopped"
    db_session.commit()
    from app.services.authorization_dr import online_abc_exception_queue as queue

    monkeypatch.setattr(queue, "preview_abc_e4", lambda *_args, **_kwargs: {"fingerprint": "a" * 64})
    monkeypatch.setattr(queue, "_e4_remote_id", lambda *_args: "remote-123")

    preview = _start_preview(db_session, batch_id)
    result = _start_apply(db_session, batch_id, preview["fingerprint"])

    item = release_tests._item(db_session, batch_id, command["account_id"])
    assert preview["current_exception"]["classification"] == "completed_checkpoint"
    assert item.status == item.outcome == "succeeded"
    assert result["sweep"]["succeeded_count"] == 1
    assert result["sweep"]["pending_count"] == len(abc_tests.ACCOUNT_IDS) - 1


def test_internal_checkpoint_does_not_stop_or_require_new_start(db_session, monkeypatch) -> None:
    batch_id = _full_batch(db_session)
    db_session.get(TgAuthorizationOnlineAbcBatch, batch_id).status = "stopped"
    db_session.commit()
    preview = _start_preview(db_session, batch_id)
    _start_apply(db_session, batch_id, preview["fingerprint"])
    from app.services.authorization_dr import online_abc_sweep as sweep

    monkeypatch.setattr(sweep, "DEFAULT_CHECKPOINT_INTERVAL", 2)
    monkeypatch.setattr(sweep, "run_next_online_abc_item", _fake_success)

    first = run_online_abc_sweep_once(db_session, runtime_release_sha=abc_tests.RELEASE_SHA)
    second = run_online_abc_sweep_once(db_session, runtime_release_sha=abc_tests.RELEASE_SHA)
    third = run_online_abc_sweep_once(db_session, runtime_release_sha=abc_tests.RELEASE_SHA)

    assert first["batch"]["status"] == second["batch"]["status"] == "sweeping"
    assert third["batch"]["status"] == "sweeping"
    assert third["sweep"]["succeeded_count"] == 3
    checkpoints = db_session.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == CHECKPOINT_ACTION,
    ))
    assert checkpoints == 1
    checkpoint = db_session.scalar(select(AuditLog).where(AuditLog.action == CHECKPOINT_ACTION))
    assert "processed_count=2;" in checkpoint.detail


def test_full_sweep_reaches_pending_zero_without_external_resume(db_session, monkeypatch) -> None:
    batch_id = _full_batch(db_session)
    db_session.get(TgAuthorizationOnlineAbcBatch, batch_id).status = "stopped"
    db_session.commit()
    preview = _start_preview(db_session, batch_id)
    _start_apply(db_session, batch_id, preview["fingerprint"])
    from app.services.authorization_dr import online_abc_sweep as sweep

    monkeypatch.setattr(sweep, "run_next_online_abc_item", _fake_success)
    result = None
    for _ in abc_tests.ACCOUNT_IDS:
        result = run_online_abc_sweep_once(db_session, runtime_release_sha=abc_tests.RELEASE_SHA)

    assert result["batch"]["status"] == "completed"
    assert result["sweep"]["pending_count"] == 0
    assert db_session.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == sweep.START_ACTION,
    )) == 1


def test_status_separates_success_manual_and_unresolved(db_session) -> None:
    batch_id, _, _ = _stopped_unknown(db_session)
    preview = _start_preview(db_session, batch_id)
    _start_apply(db_session, batch_id, preview["fingerprint"])

    status = online_abc_sweep_status(db_session, batch_id)

    counts = Counter(status["batch"]["account_outcome_counts"])
    assert counts["pending"] + counts["deferred_reconcile"] == len(abc_tests.ACCOUNT_IDS)
    assert status["sweep"]["manual_required_count"] == 0
    assert status["sweep"]["deferred_reconcile_count"] == 1
    assert status["exceptions"]["unresolved_count"] == 1


def test_same_start_is_idempotent_and_sweep_continues_after_quarantine(db_session, monkeypatch) -> None:
    batch_id, _, _ = _stopped_unknown(db_session)
    preview = _start_preview(db_session, batch_id)
    first = _start_apply(db_session, batch_id, preview["fingerprint"])
    second = _start_apply(db_session, batch_id, preview["fingerprint"])
    from app.services.authorization_dr import online_abc_sweep as sweep

    monkeypatch.setattr(sweep, "run_next_online_abc_item", _fake_success)
    result = run_online_abc_sweep_once(db_session, runtime_release_sha=abc_tests.RELEASE_SHA)
    starts = db_session.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == sweep.START_ACTION,
    ))

    assert first["already_applied"] is False
    assert second["already_applied"] is True
    assert starts == 1
    assert result["batch"]["status"] == "sweeping"
    assert result["sweep"]["deferred_reconcile_count"] == 1
    assert result["sweep"]["succeeded_count"] == 1


def test_checkpoint_pauses_if_queued_exception_a_changes(db_session, monkeypatch) -> None:
    batch_id, account_id, _ = _stopped_unknown(db_session)
    preview = _start_preview(db_session, batch_id)
    _start_apply(db_session, batch_id, preview["fingerprint"])
    account = db_session.get(TgAccount, account_id)
    account.connection_generation += 1
    db_session.commit()
    from app.services.authorization_dr import online_abc_sweep as sweep

    monkeypatch.setattr(sweep, "DEFAULT_CHECKPOINT_INTERVAL", 1)
    monkeypatch.setattr(sweep, "run_next_online_abc_item", _fake_success)

    result = run_online_abc_sweep_once(db_session, runtime_release_sha=abc_tests.RELEASE_SHA)

    assert result["batch"]["status"] == "sweep_paused"
    assert result["sweep"]["pending_count"] == len(abc_tests.ACCOUNT_IDS) - 1


def test_production_deploy_manages_single_durable_sweep_worker() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker-compose.server.yml").read_text()
    compose_up = (root / "deploy/compose-up.sh").read_text()
    check_web = (root / "deploy/check-web.sh").read_text()

    assert "  worker-authorization-abc-sweep:" in compose
    assert "container_name: tgyunying-worker-authorization-abc-sweep" in compose
    assert "scripts/authorization_online_abc_supervisor.py" in compose
    assert "  worker-authorization-abc-sweep" in compose_up
    assert "  tgyunying-worker-authorization-abc-sweep" in check_web


def test_formal_start_requires_explicit_until_exhausted(db_session) -> None:
    batch_id = _full_batch(db_session)
    db_session.get(TgAuthorizationOnlineAbcBatch, batch_id).status = "stopped"
    db_session.commit()
    from scripts import authorization_online_abc_sweep as cli

    with pytest.raises(ValueError, match="requires_until_exhausted"):
        cli._execute(db_session, SimpleNamespace(
            mode="preview", batch_id=batch_id, idempotency_key=START_KEY,
            requested_by="requester", approved_by="approver", approval_ref="ABC-FULL",
            expected_fingerprint="", until_exhausted=False,
        ))


def test_sweep_cli_is_one_shot_and_has_no_max_accounts_option() -> None:
    from scripts import authorization_online_abc_sweep as cli

    args = cli._parser().parse_args(["--mode", "sweep", "--until-exhausted"])
    assert args.mode == "sweep"
    assert args.until_exhausted is True
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["--mode", "sweep", "--until-exhausted", "--max-accounts", "30"])


def _full_batch(session) -> str:
    abc_tests._seed_accepted_canary(session)
    preview = preview_full_online_abc_batch(
        session, 1, idempotency_key="sweep-full", deployed_release_sha=abc_tests.RELEASE_SHA,
    )
    return apply_full_online_abc_batch(
        session, 1, idempotency_key="sweep-full", deployed_release_sha=abc_tests.RELEASE_SHA,
        expected_fingerprint=preview["fingerprint"], requested_by="requester",
        approved_by="approver", approval_ref="ABC-FULL",
    )["batch_id"]


def _stopped_unknown(session) -> tuple[str, int, str]:
    batch_id = _full_batch(session)
    command = start_next_online_abc_item(
        session, batch_id, actor="approver", approval_ref="ABC-FULL",
    )
    operation = abc_tests._add_operation(
        session, command["account_id"], command["b_idempotency_key"], "reconcile_unknown",
    )
    operation.blocker_code = "TimeoutError"
    operation.remote_effect_started_at = _now()
    session.commit()
    sync_online_abc_batch(session, batch_id, actor="approver", approval_ref="ABC-FULL")
    return batch_id, command["account_id"], operation.id


def _start_preview(session, batch_id: str) -> dict:
    return preview_online_abc_sweep_start(
        session, batch_id, runtime_release_sha=abc_tests.RELEASE_SHA,
        idempotency_key=START_KEY, requested_by="requester",
        approved_by="approver", approval_ref="ABC-FULL",
    )


def _start_apply(session, batch_id: str, fingerprint: str) -> dict:
    return apply_online_abc_sweep_start(
        session, batch_id, runtime_release_sha=abc_tests.RELEASE_SHA,
        idempotency_key=START_KEY, expected_fingerprint=fingerprint,
        requested_by="requester", approved_by="approver", approval_ref="ABC-FULL",
    )


def _fake_success(session, batch_id: str, **_kwargs) -> dict:
    item = session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.status == "pending",
    ).order_by(TgAuthorizationOnlineAbcItem.ordinal).limit(1))
    item.status = item.outcome = "succeeded"
    item.version += 1
    session.commit()
    return {"item_terminal": True, "processed_account_id": item.account_id}


def _a_snapshot(session, account_id: int) -> tuple:
    account = session.get(TgAccount, account_id)
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    return (
        account.current_authorization_id, account.session_ciphertext,
        account.authorization_generation, account.authorization_fact_generation,
        account.connection_generation, primary.session_ciphertext,
        primary.fact_version, primary.status, primary.health_status,
    )


def _operation_count(session, statuses: set[str]) -> int:
    return int(session.scalar(select(func.count()).select_from(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.status.in_(statuses),
    )) or 0)
