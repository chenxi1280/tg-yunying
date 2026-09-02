from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import (
    AccountStatus,
    AuditLog,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
    TgLoginFlow,
)
from app.services._common import _now
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.online_abc import start_next_online_abc_item, sync_online_abc_batch
from app.services.authorization_dr.online_abc_manifest import (
    apply_full_online_abc_batch,
    preview_full_online_abc_batch,
)
from app.services.authorization_dr.online_abc_manual_outcome import (
    MANUAL_ACTION,
    apply_manual_online_abc_outcome,
    preview_manual_online_abc_outcome,
)
import app.services.authorization_dr.online_abc_runner as runner
from tests import test_authorization_online_abc as abc_tests


pytestmark = pytest.mark.no_postgres
NEW_RELEASE_SHA = "b" * 40
MANUAL_REF = "user-message-account-manual-skip"
MANUAL_KEY = "abc-manual-outcome:test:101:v1"


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


def test_preview_is_read_only_and_freezes_manual_boundary(db_session) -> None:
    batch_id, account_id, operation_id = _stopped_manual_item(db_session)
    before = _snapshot(db_session, batch_id, account_id)

    preview = _preview(db_session, batch_id, account_id)

    assert preview["manual_stage"] == "b"
    assert preview["manual_operation_id"] == operation_id
    assert preview["manual_blocker_code"] == "two_fa_invalid"
    assert preview["primary"]["state"] == "frozen"
    assert preview["global"] == {
        "runtime_mode": "off",
        "runtime_scope": "",
        "unknown": 0,
        "sensitive": 0,
        "my_clients": 0,
    }
    assert _snapshot(db_session, batch_id, account_id) == before
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_apply_records_real_manual_debt_and_preserves_a(db_session) -> None:
    batch_id, account_id, operation_id = _stopped_manual_item(db_session)
    before_a = _a_snapshot(db_session, account_id)
    preview = _preview(db_session, batch_id, account_id)

    result = _apply(db_session, batch_id, account_id, preview["fingerprint"])

    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = _item(db_session, batch_id, account_id)
    slots = _slots(db_session, item.id)
    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    assert result["already_applied"] is False
    assert batch.status == "running"
    assert batch.execution_release_sha == NEW_RELEASE_SHA
    assert item.status == item.outcome == "manual_required"
    assert slots["standby_1"].outcome == "manual_required"
    assert slots["standby_2"].outcome == "manual_required"
    assert slots["standby_2"].operation_id is None
    assert operation.status == "manual_required"
    assert _a_snapshot(db_session, account_id) == before_a
    audit_row = db_session.scalar(select(AuditLog).where(AuditLog.action == MANUAL_ACTION))
    assert audit_row and MANUAL_KEY in audit_row.detail and preview["fingerprint"] in audit_row.detail


def test_apply_accepts_unreadable_b_code_without_rewriting_remote_fact(db_session) -> None:
    batch_id, account_id, operation_id, flow_id = _stopped_unreadable_code_item(db_session)
    before_a = _a_snapshot(db_session, account_id)

    preview = _preview(db_session, batch_id, account_id)
    result = _apply(db_session, batch_id, account_id, preview["fingerprint"])

    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    flow = db_session.get(TgLoginFlow, flow_id)
    assert preview["manual_blocker_code"] == "verification_code_unreadable"
    assert result["item_outcome"] == "manual_required"
    assert result["b_outcome"] == result["c_outcome"] == "manual_required"
    assert (operation.status, operation.remote_call_state) == ("failed", "started")
    assert flow.status == AccountStatus.WAITING_CODE.value
    assert flow.authorization_id is None
    assert _a_snapshot(db_session, account_id) == before_a


@pytest.mark.parametrize(
    "unsafe_effect",
    ["login_code_message_id", "login_code_received_at", "candidate_authorization_id", "flow_authorization"],
)
def test_preview_rejects_unreadable_b_code_with_downstream_effect(db_session, unsafe_effect) -> None:
    batch_id, account_id, operation_id, flow_id = _stopped_unreadable_code_item(db_session)
    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    flow = db_session.get(TgLoginFlow, flow_id)
    if unsafe_effect == "flow_authorization":
        flow.authorization_id = db_session.get(TgAccount, account_id).current_authorization_id
    elif unsafe_effect == "login_code_received_at":
        operation.login_code_received_at = _now()
    else:
        setattr(operation, unsafe_effect, "telegram-message" if unsafe_effect.endswith("message_id") else 999)
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)

    assert exc_info.value.code == "online_abc_manual_outcome_state_invalid"


def test_preview_rejects_unreadable_b_code_with_nonfailed_slot(db_session) -> None:
    batch_id, account_id, _, _ = _stopped_unreadable_code_item(db_session)
    item = _item(db_session, batch_id, account_id)
    _slots(db_session, item.id)["standby_1"].outcome = "manual_required"
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)

    assert exc_info.value.code == "online_abc_manual_outcome_state_invalid"


def test_apply_is_idempotent_and_rejects_key_conflict(db_session) -> None:
    batch_id, account_id, _ = _stopped_manual_item(db_session)
    preview = _preview(db_session, batch_id, account_id)

    first = _apply(db_session, batch_id, account_id, preview["fingerprint"])
    second = _apply(db_session, batch_id, account_id, preview["fingerprint"])

    assert second["already_applied"] is True
    assert second["item_version"] == first["item_version"]
    with pytest.raises(AuthorizationDrError) as exc_info:
        _apply(db_session, batch_id, account_id, "f" * 64)
    assert exc_info.value.code == "idempotency_key_conflict"


def test_apply_accepts_confirmed_c_manual_and_preserves_ready_b(db_session) -> None:
    batch_id, account_id, operation_id = _stopped_c_manual_item(db_session)
    before_a = _a_snapshot(db_session, account_id)
    item = _item(db_session, batch_id, account_id)
    before_b = _slots(db_session, item.id)["standby_1"].version
    preview = _preview(db_session, batch_id, account_id)

    result = _apply(db_session, batch_id, account_id, preview["fingerprint"])

    item = _item(db_session, batch_id, account_id)
    slots = _slots(db_session, item.id)
    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    assert preview["manual_stage"] == "c"
    assert preview["manual_operation_id"] == operation_id
    assert result["b_outcome"] == "already_qualified"
    assert result["c_outcome"] == "manual_required"
    assert slots["standby_1"].version == before_b
    assert operation.status == "manual_required"
    assert _a_snapshot(db_session, account_id) == before_a


def test_preview_rejects_a_drift_and_non_no_effect_operation(db_session) -> None:
    batch_id, account_id, operation_id = _stopped_manual_item(db_session)
    account = db_session.get(TgAccount, account_id)
    account.connection_generation += 1
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)
    assert exc_info.value.code == "online_abc_primary_drift"

    account.connection_generation -= 1
    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    operation.remote_call_state = "unknown"
    db_session.commit()
    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)
    assert exc_info.value.code == "online_abc_manual_outcome_state_invalid"


def test_manual_item_does_not_block_later_chunks_or_fake_full_success(
    db_session, monkeypatch,
) -> None:
    batch_id, account_id, _ = _stopped_manual_item(db_session)
    preview = _preview(db_session, batch_id, account_id)
    _apply(db_session, batch_id, account_id, preview["fingerprint"])
    calls: list[tuple[str, int]] = []
    abc_tests._mock_runner_success(monkeypatch, calls)

    first = _run(db_session, batch_id, max_accounts=2)
    final = _run(db_session, batch_id, max_accounts=10)

    assert first["chunk"]["account_ids"] == abc_tests.ACCOUNT_IDS[1:3]
    assert first["batch"]["status"] == "stopped"
    assert final["batch"]["status"] == "completed_with_manual"
    assert final["batch"]["account_outcome_counts"] == {
        "manual_required": 1,
        "succeeded": 9,
    }
    assert account_id not in [value for _, value in calls]


def test_manual_a_drift_stops_before_next_account(db_session, monkeypatch) -> None:
    batch_id, account_id, _ = _stopped_manual_item(db_session)
    preview = _preview(db_session, batch_id, account_id)
    _apply(db_session, batch_id, account_id, preview["fingerprint"])
    account = db_session.get(TgAccount, account_id)
    account.connection_generation += 1
    db_session.commit()
    abc_tests._mock_runner_success(monkeypatch, [])

    with pytest.raises(AuthorizationDrError) as exc_info:
        _run(db_session, batch_id, max_accounts=2)

    assert exc_info.value.code == "online_abc_primary_drift"
    assert db_session.get(TgAuthorizationOnlineAbcBatch, batch_id).status == "stopped"
    assert _item(db_session, batch_id, abc_tests.ACCOUNT_IDS[1]).status == "pending"


def _stopped_manual_item(session) -> tuple[str, int, str]:
    batch_id = _new_full_batch(session, "full-manual-test")
    command = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-FULL")
    operation = abc_tests._add_operation(
        session, command["account_id"], command["b_idempotency_key"], "manual_required",
    )
    operation.remote_call_state = "confirmed_no_effect"
    operation.blocker_code = "two_fa_invalid"
    session.commit()
    sync_online_abc_batch(session, batch_id, actor="approver", approval_ref="ABC-FULL")
    return batch_id, command["account_id"], operation.id


def _stopped_unreadable_code_item(session) -> tuple[str, int, str, int]:
    batch_id = _new_full_batch(session, "full-unreadable-code-test")
    command = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-FULL")
    operation = abc_tests._add_operation(
        session, command["account_id"], command["b_idempotency_key"], "failed",
    )
    flow = TgLoginFlow(
        tenant_id=1,
        account_id=command["account_id"],
        method="code",
        status=AccountStatus.WAITING_CODE.value,
        authorization_role="standby_1",
        developer_app_id=1,
        challenge_sent_at=_now(),
        code_expires_at=_now() + timedelta(minutes=5),
    )
    session.add(flow)
    session.flush()
    operation.remote_call_state = "started"
    operation.blocker_code = "verification_code_unreadable"
    operation.remote_effect_started_at = _now()
    operation.login_challenge_sent_at = _now()
    operation.login_flow_id = flow.id
    operation.login_code_message_id = ""
    operation.login_code_received_at = None
    operation.candidate_authorization_id = None
    session.commit()
    sync_online_abc_batch(session, batch_id, actor="approver", approval_ref="ABC-FULL")
    return batch_id, command["account_id"], operation.id, flow.id


def _stopped_c_manual_item(session) -> tuple[str, int, str]:
    batch_id = _new_full_batch(session, "full-c-manual-test")
    command = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-FULL")
    item = _item(session, batch_id, command["account_id"])
    item.standby_1_plan = "already_qualified"
    _slots(session, item.id)["standby_1"].outcome = "already_qualified"
    session.commit()
    abc_tests._qualify_primary(session, command["account_id"])
    c_result = abc_tests._add_c_operation(
        session, command["account_id"], command["c_idempotency_key"], "manual_required",
    )
    operation = session.get(TgAuthorizationDrOperation, c_result["operation_id"])
    operation.remote_call_state = "confirmed_no_effect"
    operation.blocker_code = "two_fa_invalid"
    session.commit()
    sync_online_abc_batch(session, batch_id, actor="approver", approval_ref="ABC-FULL")
    return batch_id, command["account_id"], operation.id


def _new_full_batch(session, key: str) -> str:
    abc_tests._seed_accepted_canary(session)
    preview = preview_full_online_abc_batch(
        session, 1, idempotency_key=key, deployed_release_sha=abc_tests.RELEASE_SHA,
    )
    return apply_full_online_abc_batch(
        session,
        1,
        idempotency_key=key,
        deployed_release_sha=abc_tests.RELEASE_SHA,
        expected_fingerprint=preview["fingerprint"],
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-FULL",
    )["batch_id"]


def _preview(session, batch_id: str, account_id: int) -> dict:
    return preview_manual_online_abc_outcome(
        session,
        batch_id,
        account_id,
        runtime_release_sha=NEW_RELEASE_SHA,
        idempotency_key=MANUAL_KEY,
        requested_by="requester",
        approved_by="approver",
        approval_ref=MANUAL_REF,
    )


def _apply(session, batch_id: str, account_id: int, fingerprint: str) -> dict:
    return apply_manual_online_abc_outcome(
        session,
        batch_id,
        account_id,
        runtime_release_sha=NEW_RELEASE_SHA,
        idempotency_key=MANUAL_KEY,
        expected_fingerprint=fingerprint,
        requested_by="requester",
        approved_by="approver",
        approval_ref=MANUAL_REF,
    )


def _run(session, batch_id: str, *, max_accounts: int) -> dict:
    return runner.run_online_abc_batch(
        session,
        batch_id,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-FULL",
        runtime_release_sha=NEW_RELEASE_SHA,
        max_accounts=max_accounts,
        sleeper=lambda _: None,
    )


def _a_snapshot(session, account_id: int) -> tuple:
    account = session.get(TgAccount, account_id)
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    return (
        account.current_authorization_id,
        account.session_ciphertext,
        account.authorization_generation,
        account.authorization_fact_generation,
        account.connection_generation,
        primary.session_ciphertext,
        primary.fact_version,
        primary.status,
        primary.health_status,
    )


def _snapshot(session, batch_id: str, account_id: int) -> tuple:
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = _item(session, batch_id, account_id)
    slots = _slots(session, item.id)
    return (
        batch.status,
        batch.version,
        batch.execution_release_sha,
        item.status,
        item.outcome,
        item.version,
        slots["standby_1"].outcome,
        slots["standby_1"].version,
        slots["standby_2"].outcome,
        slots["standby_2"].version,
        _a_snapshot(session, account_id),
    )


def _item(session, batch_id: str, account_id: int):
    return session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ))


def _slots(session, item_id: str) -> dict:
    values = session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item_id,
    ))
    return {value.logical_slot: value for value in values}
