from __future__ import annotations

import pytest

import app.services.authorization_dr.online_abc_runner as runner
import app.services.authorization_dr.standby_1_qualification as standby_qualification
from app.integrations.telegram.contracts import AuthorizationIdentity
from app.models import TgAccount, TgAccountAuthorization, TgAuthorizationOnlineAbcBatch
from app.services.authorization_dr.contracts import AuthorizationDrError
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


def test_resume_reopens_pre_primary_value_error_without_operations(db_session, monkeypatch) -> None:
    batch_id, item = _stopped_before_primary(db_session)
    monkeypatch.setattr(
        runner,
        "ready_migration_runtime_image_sha",
        lambda _session: pytest.fail("pre-primary resume must not require MY runtime"),
    )
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
    assert result["current_item"] == {
        "id": item.id,
        "ordinal": item.ordinal,
        "account_id": item.account_id,
        "status": "running",
    }
    assert result["operations"] == {"b": None, "c": None, "e4": None}
    assert result["batch"]["execution_release_sha"] == resumed_release_sha


def test_resume_rejects_pre_primary_value_error_after_operation_created(db_session) -> None:
    batch_id, item = _stopped_before_primary(db_session)
    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    key = runner.online_abc_operation_keys(batch, item)["b"]
    abc_tests._add_operation(db_session, item.account_id, key, "succeeded")

    with pytest.raises(AuthorizationDrError) as exc_info:
        runner.resume_online_abc_batch(
            db_session,
            batch_id,
            requested_by="requester",
            approved_by="approver",
            approval_ref="ABC-10",
            runtime_release_sha=abc_tests.RELEASE_SHA,
        )

    assert exc_info.value.code == "online_abc_resume_remote_effect_started"
    assert db_session.get(TgAuthorizationOnlineAbcBatch, batch_id).status == "stopped"


def test_resume_rejects_pre_primary_value_error_after_a_drift(db_session) -> None:
    batch_id, item = _stopped_before_primary(db_session)
    account = db_session.get(TgAccount, item.account_id)
    account.authorization_generation += 1
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        runner.resume_online_abc_batch(
            db_session,
            batch_id,
            requested_by="requester",
            approved_by="approver",
            approval_ref="ABC-10",
            runtime_release_sha=abc_tests.RELEASE_SHA,
        )

    assert exc_info.value.code == "online_abc_primary_drift"
    assert db_session.get(TgAuthorizationOnlineAbcBatch, batch_id).status == "stopped"


def test_resume_rejects_global_provision_reconcile_unknown(db_session, monkeypatch) -> None:
    batch_id, _item, _operation_ids = _stop_after_c(db_session, "malaysia_wake_unavailable")
    abc_tests._add_operation(
        db_session,
        abc_tests.ACCOUNT_IDS[1],
        "global-provision-unknown",
        "provision_reconcile_unknown",
    )
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

    assert exc_info.value.code == "global_reconcile_unknown"


def test_resume_reopens_reconciled_b_before_primary_qualification(db_session, monkeypatch) -> None:
    batch_id, item = _stopped_before_primary(db_session)
    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    primary = db_session.get(TgAccountAuthorization, item.primary_authorization_id)
    primary.telegram_user_id_digest = "1" * 64
    primary.auth_key_fingerprint_digest = "2" * 64
    candidate = TgAccountAuthorization(
        tenant_id=1, account_id=item.account_id, role="standby_1", logical_slot="primary",
        provision_region_code="sv", developer_app_id=1, session_ciphertext="recovered-b",
        status="standby", health_status="healthy", is_current=False, is_slot_current=True,
        telegram_user_id_digest=primary.telegram_user_id_digest,
        auth_key_fingerprint_digest="9" * 64,
    )
    db_session.add(candidate)
    db_session.flush()
    operation = abc_tests._add_operation(
        db_session,
        item.account_id,
        runner.online_abc_operation_keys(batch, item)["b"],
        "succeeded",
    )
    operation.candidate_authorization_id = candidate.id
    operation.reconcile_status = "applied"
    operation.reconcile_case_id = "reconcile-case"
    item.outcome = "reconcile_unknown"
    item.blocker_code = "reconcile_unknown"
    db_session.commit()

    result = runner.resume_online_abc_batch(
        db_session,
        batch_id,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
        runtime_release_sha="2" * 40,
    )

    assert result["batch"]["status"] == "running"
    assert result["current_item"]["id"] == item.id
    assert result["operations"]["b"]["status"] == "succeeded"


def test_resume_reopens_artifact_reconciled_c_without_replaying_c(db_session, monkeypatch) -> None:
    batch_id, item, _operation_ids = _running_after_c(db_session)
    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    operation = runner._context(db_session, batch_id)[2]["c"]
    operation.remote_call_state = "confirmed"
    operation.reconcile_status = "applied"
    operation.reconcile_case_id = "artifact-reconcile-case"
    operation.candidate_authorization_id = item.source_c_authorization_id
    item.status = "stopped"
    item.outcome = "reconcile_unknown"
    item.blocker_code = "reconcile_unknown"
    batch.status = "stopped"
    db_session.commit()
    monkeypatch.setattr(runner, "ready_migration_runtime_image_sha", lambda _session: "d" * 40)

    result = runner.resume_online_abc_batch(
        db_session,
        batch_id,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
        runtime_release_sha="2" * 40,
    )

    assert result["batch"]["status"] == "running"
    assert result["current_item"]["id"] == item.id
    assert result["operations"]["c"]["id"] == operation.id
    assert result["operations"]["e4"] is None


def test_resume_rejects_unreconciled_c_unknown_item(db_session, monkeypatch) -> None:
    batch_id, item, _operation_ids = _running_after_c(db_session)
    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item.status = "stopped"
    item.outcome = "reconcile_unknown"
    item.blocker_code = "reconcile_unknown"
    batch.status = "stopped"
    db_session.commit()
    monkeypatch.setattr(runner, "ready_migration_runtime_image_sha", lambda _session: "d" * 40)

    with pytest.raises(AuthorizationDrError) as exc_info:
        runner.resume_online_abc_batch(
            db_session,
            batch_id,
            requested_by="requester",
            approved_by="approver",
            approval_ref="ABC-10",
            runtime_release_sha="2" * 40,
        )

    assert exc_info.value.code == "online_abc_resume_remote_effect_started"
    assert db_session.get(TgAuthorizationOnlineAbcBatch, batch_id).status == "stopped"


def test_resume_reopens_succeeded_b_before_c_without_replaying_b(db_session) -> None:
    batch_id, item, operation = _stopped_after_b(db_session, "sv_redundancy_incomplete")

    result = runner.resume_online_abc_batch(
        db_session,
        batch_id,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
        runtime_release_sha="2" * 40,
    )

    assert result["batch"]["status"] == "running"
    assert result["current_item"]["id"] == item.id
    assert result["operations"]["b"]["id"] == operation.id
    assert result["operations"]["c"] is None
    assert result["operations"]["e4"] is None


def test_resume_reopens_succeeded_b_after_my_readiness_failure(db_session, monkeypatch) -> None:
    batch_id, item, operation = _stopped_after_b(db_session, "malaysia_wake_unavailable")
    readiness_calls = 0

    def ready(_session):
        nonlocal readiness_calls
        readiness_calls += 1
        return "d" * 40

    monkeypatch.setattr(runner, "ready_migration_runtime_image_sha", ready)

    result = runner.resume_online_abc_batch(
        db_session,
        batch_id,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
        runtime_release_sha="2" * 40,
    )

    assert readiness_calls == 1
    assert result["batch"]["status"] == "running"
    assert result["current_item"]["id"] == item.id
    assert result["operations"]["b"]["id"] == operation.id
    assert result["operations"]["c"] is None
    assert result["operations"]["e4"] is None


def test_resume_my_readiness_failure_before_c_is_zero_write(db_session, monkeypatch) -> None:
    batch_id, item, _operation = _stopped_after_b(db_session, "malaysia_wake_unavailable")
    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    batch_version = batch.version
    item_version = item.version

    def unavailable(_session):
        raise AuthorizationDrError("malaysia_wake_unavailable", "MY execution node heartbeat is stale")

    monkeypatch.setattr(runner, "ready_migration_runtime_image_sha", unavailable)

    with pytest.raises(AuthorizationDrError) as exc_info:
        runner.resume_online_abc_batch(
            db_session,
            batch_id,
            requested_by="requester",
            approved_by="approver",
            approval_ref="ABC-10",
            runtime_release_sha="2" * 40,
        )

    assert exc_info.value.code == "malaysia_wake_unavailable"
    db_session.refresh(batch)
    db_session.refresh(item)
    assert batch.status == "stopped"
    assert batch.version == batch_version
    assert item.status == "stopped"
    assert item.version == item_version


def test_resume_reopens_post_c_existing_b_qualification_without_replaying_c(
    db_session, monkeypatch,
) -> None:
    batch_id, item, standby, c_operation_id = _stopped_after_c_with_legacy_b(db_session)
    monkeypatch.setattr(runner, "ready_migration_runtime_image_sha", lambda _session: "d" * 40)

    result = runner.resume_online_abc_batch(
        db_session,
        batch_id,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
        runtime_release_sha="2" * 40,
    )

    assert result["batch"]["status"] == "running"
    assert result["current_item"]["id"] == item.id
    assert result["operations"]["b"] is None
    assert result["operations"]["c"]["id"] == c_operation_id
    assert result["operations"]["e4"] is None
    assert standby.telegram_user_id_digest == ""


def test_existing_b_qualification_updates_only_b_identity(db_session, monkeypatch) -> None:
    _batch_id, item, standby, _c_operation_id = _stopped_after_c_with_legacy_b(db_session)
    account = db_session.get(TgAccount, item.account_id)
    primary = db_session.get(TgAccountAuthorization, item.primary_authorization_id)
    a_before = (
        account.current_authorization_id,
        account.session_ciphertext,
        account.authorization_generation,
        account.authorization_fact_generation,
        account.connection_generation,
        primary.fact_version,
    )
    identity = AuthorizationIdentity("0", "9" * 64, primary.telegram_user_id_digest, "f" * 64)
    resolved = AuthorizationIdentity("12345", "9" * 64, primary.telegram_user_id_digest, "f" * 64)
    monkeypatch.setattr(standby_qualification.gateway, "authorization_identity", lambda *_args: identity)
    monkeypatch.setattr(
        standby_qualification,
        "resolve_authorization_identity_hash",
        lambda *_args, **_kwargs: (resolved, "peer_observer"),
    )

    result = standby_qualification.qualify_existing_standby_1(
        db_session,
        item,
        actor="approver",
        approval_ref="ABC-10",
    )

    db_session.refresh(account)
    db_session.refresh(primary)
    db_session.refresh(standby)
    assert result == {"authorization_id": standby.id, "status": "qualified"}
    assert standby.telegram_user_id_digest == primary.telegram_user_id_digest
    assert standby.auth_key_fingerprint_digest == "9" * 64
    assert standby.fact_version == 2
    assert a_before == (
        account.current_authorization_id,
        account.session_ciphertext,
        account.authorization_generation,
        account.authorization_fact_generation,
        account.connection_generation,
        primary.fact_version,
    )


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


def test_c_wait_stops_on_provision_reconcile_unknown(db_session) -> None:
    batch_id, _item, _operation_ids = _running_after_c(db_session)
    operation = runner._context(db_session, batch_id)[2]["c"]
    operation.status = "provision_reconcile_unknown"
    db_session.commit()

    runner._wait_for_c(
        db_session,
        batch_id,
        0.01,
        lambda _seconds: pytest.fail("terminal C unknown must not be polled"),
    )


def _stop_after_c(session, blocker_code: str):
    batch_id, item, operation_ids = _running_after_c(session)
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item.status = "stopped"
    item.outcome = "runner_blocked"
    item.blocker_code = blocker_code
    batch.status = "stopped"
    session.commit()
    return batch_id, item, operation_ids


def _stopped_before_primary(session):
    batch_id = abc_tests._apply(session, abc_tests._preview(session)["fingerprint"])["batch_id"]
    command = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-10")
    item = runner._context(session, batch_id)[1]
    assert item.id == command["item_id"]
    account = session.get(TgAccount, item.account_id)
    current = session.get(TgAccountAuthorization, account.current_authorization_id)
    current.logical_slot = "standby_1"
    current.role = "standby_1"
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item.status = "stopped"
    item.outcome = "runner_blocked"
    item.blocker_code = "ValueError"
    batch.status = "stopped"
    session.commit()
    return batch_id, item


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


def _stopped_after_b(session, blocker_code: str):
    batch_id, item = _stopped_before_primary(session)
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    abc_tests._qualify_primary(session, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    candidate = TgAccountAuthorization(
        tenant_id=1, account_id=item.account_id, role="standby_1", logical_slot="primary",
        provision_region_code="sv", developer_app_id=1, session_ciphertext="new-b",
        status="standby", health_status="healthy", is_current=False, is_slot_current=True,
        telegram_user_id_digest=primary.telegram_user_id_digest,
        auth_key_fingerprint_digest="9" * 64,
    )
    session.add(candidate)
    session.flush()
    operation = abc_tests._add_operation(
        session, item.account_id, runner.online_abc_operation_keys(batch, item)["b"], "succeeded",
    )
    operation.candidate_authorization_id = candidate.id
    item.blocker_code = blocker_code
    session.commit()
    return batch_id, item, operation


def _stopped_after_c_with_legacy_b(session):
    batch_id = abc_tests._apply(session, abc_tests._preview(session)["fingerprint"])["batch_id"]
    command = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-10")
    item = runner._context(session, batch_id)[1]
    abc_tests._qualify_primary(session, command["account_id"])
    standby = TgAccountAuthorization(
        tenant_id=1,
        account_id=item.account_id,
        role="standby_1",
        logical_slot="standby_1",
        provision_region_code="sv",
        developer_app_id=item.app_b_id,
        proxy_id=item.proxy_id,
        session_ciphertext="legacy-b",
        telegram_authorization_hash_ciphertext="legacy-hash",
        status="standby",
        health_status="healthy",
        is_current=False,
        is_slot_current=True,
        protected_from_cleanup=True,
    )
    session.add(standby)
    item.standby_1_plan = "already_qualified"
    c = abc_tests._add_c_operation(
        session,
        item.account_id,
        command["c_idempotency_key"],
        "succeeded",
    )
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item.status = "stopped"
    item.outcome = "runner_blocked"
    item.blocker_code = "sv_redundancy_incomplete"
    batch.status = "stopped"
    session.commit()
    return batch_id, item, standby, c["operation_id"]
