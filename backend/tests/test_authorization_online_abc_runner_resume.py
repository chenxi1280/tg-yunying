from __future__ import annotations

import pytest

from app.models import TgAuthorizationOnlineAbcBatch
from app.services.authorization_dr.contracts import AuthorizationDrError
import app.services.authorization_dr.online_abc_runner as runner
from app.services.authorization_dr.online_abc import start_next_online_abc_item
from tests import test_authorization_online_abc as abc_tests


pytestmark = pytest.mark.no_postgres


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


def test_resume_reopens_only_post_c_pre_e4_checkpoint(db_session, monkeypatch) -> None:
    batch_id, item, operation_ids = _stop_after_c(db_session, "malaysia_wake_unavailable")
    monkeypatch.setattr(runner, "ready_migration_runtime_image_sha", lambda _session: "d" * 40)
    resumed_release_sha = "2" * 40

    result = runner.resume_online_abc_batch(
        db_session,
        batch_id,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
        runtime_release_sha=resumed_release_sha,
    )

    assert result["batch"]["status"] == "running"
    assert result["current_item"]["id"] == item.id
    assert result["current_item"]["status"] == "running"
    assert {value["id"] for value in result["operations"].values() if value} == operation_ids
    assert result["operations"]["e4"] is None
    assert result["batch"]["deployed_release_sha"] == abc_tests.RELEASE_SHA
    assert result["batch"]["execution_release_sha"] == resumed_release_sha


def test_resume_rejects_non_allowlisted_blocker(db_session, monkeypatch) -> None:
    batch_id, _item, _operation_ids = _stop_after_c(db_session, "RuntimeError")
    monkeypatch.setattr(runner, "ready_migration_runtime_image_sha", lambda _session: "d" * 40)

    with pytest.raises(AuthorizationDrError) as exc_info:
        runner.resume_online_abc_batch(
            db_session,
            batch_id,
            requested_by="requester",
            approved_by="approver",
            approval_ref="ABC-10",
            runtime_release_sha=abc_tests.RELEASE_SHA,
        )

    assert exc_info.value.code == "online_abc_resume_blocker_forbidden"
    assert db_session.get(TgAuthorizationOnlineAbcBatch, batch_id).status == "stopped"


def test_e4_waits_for_transient_my_readiness(db_session, monkeypatch) -> None:
    batch_id, item, _operation_ids = _running_after_c(db_session)
    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    attempts = 0
    sleeps: list[float] = []

    def preview(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AuthorizationDrError("malaysia_wake_unavailable", "heartbeat refresh pending")
        return {"fingerprint": "e" * 64}

    monkeypatch.setattr(runner, "preview_abc_e4", preview)
    monkeypatch.setattr(
        runner,
        "apply_abc_e4",
        lambda db, _tenant_id, account_id, **kwargs: abc_tests._add_operation(
            db, account_id, kwargs["idempotency_key"], "succeeded",
        ),
    )

    runner._create_e4(
        db_session,
        batch,
        item,
        runner.RunnerApproval("requester", "approver", "ABC-10"),
        0.01,
        sleeps.append,
    )

    assert attempts == 2
    assert sleeps == [0.01]
    assert runner._context(db_session, batch_id)[2]["e4"].status == "succeeded"


def _stop_after_c(session, blocker_code: str):
    batch_id, item, operation_ids = _running_after_c(session)
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item.status = "stopped"
    item.outcome = "runner_blocked"
    item.blocker_code = blocker_code
    batch.status = "stopped"
    session.commit()
    return batch_id, item, operation_ids


def _running_after_c(session):
    batch_id = abc_tests._apply(session, abc_tests._preview(session)["fingerprint"])["batch_id"]
    command = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-10")
    abc_tests._qualify_primary(session, command["account_id"])
    b = abc_tests._add_operation(
        session, command["account_id"], command["b_idempotency_key"], "succeeded",
    )
    c = abc_tests._add_c_operation(
        session, command["account_id"], command["c_idempotency_key"], "succeeded",
    )
    item = runner._context(session, batch_id)[1]
    return batch_id, item, {b.id, c["operation_id"]}
