from __future__ import annotations

import pytest

import app.services.authorization_dr.online_abc_runner as runner
from app.models import TgAccountAuthorization, TgAuthorizationOnlineAbcBatch
from app.services.authorization_dr.contracts import AuthorizationDrError
from tests import test_authorization_online_abc as abc_tests
from tests import test_authorization_online_abc_runner_resume as resume_tests


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


def test_resume_reopens_succeeded_b_after_my_readiness_failure(db_session, monkeypatch) -> None:
    batch_id, item, operation = _stopped_after_b(db_session)
    readiness_calls = 0

    def ready(_session):
        nonlocal readiness_calls
        readiness_calls += 1
        return "d" * 40

    monkeypatch.setattr(runner, "ready_migration_runtime_image_sha", ready)

    result = _resume(db_session, batch_id)

    assert readiness_calls == 1
    assert result["batch"]["status"] == "running"
    assert result["current_item"]["id"] == item.id
    assert result["operations"]["b"]["id"] == operation.id
    assert result["operations"]["c"] is None
    assert result["operations"]["e4"] is None


def test_resume_my_readiness_failure_before_c_is_zero_write(db_session, monkeypatch) -> None:
    batch_id, item, _operation = _stopped_after_b(db_session)
    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    batch_version = batch.version
    item_version = item.version

    def unavailable(_session):
        raise AuthorizationDrError("malaysia_wake_unavailable", "MY execution node heartbeat is stale")

    monkeypatch.setattr(runner, "ready_migration_runtime_image_sha", unavailable)

    with pytest.raises(AuthorizationDrError) as exc_info:
        _resume(db_session, batch_id)

    assert exc_info.value.code == "malaysia_wake_unavailable"
    db_session.refresh(batch)
    db_session.refresh(item)
    assert (batch.status, batch.version) == ("stopped", batch_version)
    assert (item.status, item.version) == ("stopped", item_version)


def _resume(session, batch_id: str) -> dict:
    return runner.resume_online_abc_batch(
        session,
        batch_id,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
        runtime_release_sha="2" * 40,
    )


def _stopped_after_b(session):
    batch_id, item = resume_tests._stopped_before_primary(session)
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    abc_tests._qualify_primary(session, item.account_id)
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    candidate = _add_candidate(session, item, primary)
    operation = abc_tests._add_operation(
        session, item.account_id, runner.online_abc_operation_keys(batch, item)["b"], "succeeded",
    )
    operation.candidate_authorization_id = candidate.id
    item.status = "stopped"
    item.outcome = "runner_blocked"
    item.blocker_code = "malaysia_wake_unavailable"
    batch.status = "stopped"
    session.commit()
    return batch_id, item, operation


def _add_candidate(session, item, primary):
    candidate = TgAccountAuthorization(
        tenant_id=1, account_id=item.account_id, role="standby_1", logical_slot="primary",
        provision_region_code="sv", developer_app_id=1, session_ciphertext="new-b",
        status="standby", health_status="healthy", is_current=False, is_slot_current=True,
        telegram_user_id_digest=primary.telegram_user_id_digest,
        auth_key_fingerprint_digest="9" * 64,
    )
    session.add(candidate)
    session.flush()
    return candidate
