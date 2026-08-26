from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import AuditLog, TgAuthorizationOnlineAbcBatch, TgAuthorizationOnlineAbcItem
from app.services.authorization_dr.online_abc import start_next_online_abc_item
from app.services.authorization_dr.online_abc_deferred_recovery import (
    FINAL_ACTION,
    ITEM_ACTION,
    apply_deferred_recovery_start,
    preview_deferred_recovery_start,
    run_deferred_recovery_once,
)
from tests import test_authorization_online_abc as abc_tests
from tests import test_authorization_online_abc_sweep as sweep_tests


pytestmark = pytest.mark.no_postgres
RECOVERY_KEY = "abc-deferred:test:v1"


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


def test_deferred_recovery_audits_remote_unknown_without_success_or_manual(db_session) -> None:
    batch_id, deferred_account, manual_account = _phase2_batch(db_session)
    preview = _preview(db_session, batch_id, expected_deferred_count=1)
    started = _apply(db_session, batch_id, preview["fingerprint"], expected_deferred_count=1)

    first = run_deferred_recovery_once(db_session, runtime_release_sha=abc_tests.RELEASE_SHA)
    second = run_deferred_recovery_once(db_session, runtime_release_sha=abc_tests.RELEASE_SHA)

    deferred = _item(db_session, batch_id, deferred_account)
    manual = _item(db_session, batch_id, manual_account)
    assert preview["deferred_count"] == 1
    assert started["deferred_recovery"]["active"] is True
    assert first["last_item"]["result"] == "deferred_reconcile"
    assert first["last_item"]["reason"] == "same_operation_remote_unknown"
    assert deferred.status == deferred.outcome == "deferred_reconcile"
    assert manual.status == manual.outcome == "manual_required"
    assert second["batch"]["status"] == "completed_with_exceptions"
    assert second["deferred_recovery"]["processed_count"] == 1
    assert second["deferred_recovery"]["remaining_deferred_to_rejudge"] == 0
    assert _audit_count(db_session, ITEM_ACTION) == 1
    assert _audit_count(db_session, FINAL_ACTION) == 1


def test_deferred_recovery_rejects_drifted_manifest_count(db_session) -> None:
    batch_id, _deferred_account, _manual_account = _phase2_batch(db_session)

    with pytest.raises(Exception, match="Deferred target count changed"):
        _preview(db_session, batch_id, expected_deferred_count=2)


def test_deferred_recovery_start_rebinds_completed_exception_batch_release(db_session) -> None:
    batch_id, _deferred_account, _manual_account = _phase2_batch(db_session)
    previous_sha = "1" * 40
    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    batch.execution_release_sha = previous_sha
    db_session.commit()

    preview = _preview(db_session, batch_id, expected_deferred_count=1)
    started = _apply(db_session, batch_id, preview["fingerprint"], expected_deferred_count=1)

    rebound = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    assert preview["previous_execution_release_sha"] == previous_sha
    assert preview["execution_release_rebind_required"] is True
    assert rebound.execution_release_sha == abc_tests.RELEASE_SHA
    assert started["deferred_recovery"]["active"] is True


def test_completed_checkpoint_forward_counts_as_real_success(db_session, monkeypatch) -> None:
    batch_id, account_id = _completed_deferred_item(db_session)
    from app.services.authorization_dr import online_abc_deferred_recovery as recovery

    monkeypatch.setattr(recovery, "preview_abc_e4", lambda *_args, **_kwargs: {"fingerprint": "a" * 64})
    monkeypatch.setattr(recovery, "e4_remote_id", lambda *_args: "remote-123")
    monkeypatch.setattr(recovery, "require_exception_primaries_unchanged", lambda *_args: None)
    preview = _preview(db_session, batch_id, expected_deferred_count=1)
    _apply(db_session, batch_id, preview["fingerprint"], expected_deferred_count=1)

    result = run_deferred_recovery_once(db_session, runtime_release_sha=abc_tests.RELEASE_SHA)

    item = _item(db_session, batch_id, account_id)
    assert result["last_item"]["result"] == "succeeded"
    assert item.status == item.outcome == "succeeded"


def test_deferred_recovery_cli_requires_until_exhausted(db_session) -> None:
    batch_id, _deferred_account, _manual_account = _phase2_batch(db_session)
    from scripts import authorization_online_abc_deferred_recovery as cli

    with pytest.raises(ValueError, match="requires_until_exhausted"):
        cli._execute(db_session, SimpleNamespace(
            mode="preview", batch_id=batch_id, idempotency_key=RECOVERY_KEY,
            expected_deferred_count=1, requested_by="requester",
            approved_by="approver", approval_ref="ABC-FULL",
            expected_fingerprint="", until_exhausted=False,
        ))


def test_compose_worker_uses_combined_abc_supervisor() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker-compose.server.yml").read_text()

    assert "container_name: tgyunying-worker-authorization-abc-sweep" in compose
    assert "scripts/authorization_online_abc_supervisor.py" in compose
    assert "authorization_online_abc_sweep.py\", \"--mode\", \"worker" not in compose


def _phase2_batch(session) -> tuple[str, int, int]:
    batch_id, deferred_account, _operation_id = sweep_tests._stopped_unknown(session)
    preview = sweep_tests._start_preview(session, batch_id)
    sweep_tests._start_apply(session, batch_id, preview["fingerprint"])
    manual_account = _finish_rest(session, batch_id, exclude_account_id=deferred_account)
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    batch.status = "completed_with_exceptions"
    session.commit()
    return batch_id, deferred_account, manual_account


def _completed_deferred_item(session) -> tuple[str, int]:
    batch_id = sweep_tests._full_batch(session)
    command = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-FULL")
    abc_tests._qualify_primary(session, command["account_id"])
    abc_tests._add_operations(session, command, status="succeeded")
    item = _item(session, batch_id, command["account_id"])
    item.status = item.outcome = "deferred_reconcile"
    item.blocker_code = "completed_checkpoint_unproven"
    for other in session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.id != item.id,
    )):
        other.status = other.outcome = "succeeded"
    session.get(TgAuthorizationOnlineAbcBatch, batch_id).status = "completed_with_exceptions"
    session.commit()
    return batch_id, command["account_id"]


def _finish_rest(session, batch_id: str, *, exclude_account_id: int) -> int:
    manual_account = 0
    for item in session.scalars(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id != exclude_account_id,
    ).order_by(TgAuthorizationOnlineAbcItem.ordinal)):
        if not manual_account:
            item.status = item.outcome = "manual_required"
            item.blocker_code = "existing_manual_debt"
            manual_account = item.account_id
        else:
            item.status = item.outcome = "succeeded"
    return manual_account


def _preview(session, batch_id: str, *, expected_deferred_count: int) -> dict:
    return preview_deferred_recovery_start(
        session, batch_id, runtime_release_sha=abc_tests.RELEASE_SHA,
        idempotency_key=RECOVERY_KEY, expected_deferred_count=expected_deferred_count,
        requested_by="requester", approved_by="approver", approval_ref="ABC-FULL",
    )


def _apply(session, batch_id: str, fingerprint: str, *, expected_deferred_count: int) -> dict:
    return apply_deferred_recovery_start(
        session, batch_id, runtime_release_sha=abc_tests.RELEASE_SHA,
        idempotency_key=RECOVERY_KEY, expected_deferred_count=expected_deferred_count,
        expected_fingerprint=fingerprint, requested_by="requester",
        approved_by="approver", approval_ref="ABC-FULL",
    )


def _item(session, batch_id: str, account_id: int):
    return session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ))


def _audit_count(session, action: str) -> int:
    return len(list(session.scalars(select(AuditLog).where(AuditLog.action == action))))
