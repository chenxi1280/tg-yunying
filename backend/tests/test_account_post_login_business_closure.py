from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import (
    TgAccountFullInitialization,
    TgAccountLoginBatchAttempt,
    TgAccountLoginBatchNotification,
    TgAccountSecurityBatch,
    TgAccountSecurityBatchItem,
    TgPostLoginAbcRequest,
)
from app.services.account_login.notifications import finalize_batch_if_terminal
from app.services.account_login.batches import cancel_login_batch, retry_login_batch_items
from app.services.account_login.state import PhaseClaim
from app.services.account_post_login_init.binding import create_or_attach_full_initialization
from app.services.account_post_login_init.parent import sync_parent_bindings
from app.services.account_post_login_init.profile import _profile_payload
from app.services.account_post_login_init.reconcile import (
    execute_reconcile_stage,
    request_post_login_reconciliation,
    request_two_fa_reset,
    submit_two_fa_candidate,
)
from app.services.account_post_login_init.contracts import FullInitializationClaim
from app.security import decrypt_secret
from app.schemas.account_login import LoginBatchRetryRequest
from app.services.account_login.contracts import BatchLoginError
from tests.test_account_post_login_full_init import _new_login_item, session_factory


pytestmark = pytest.mark.no_postgres


def _terminal_item(session, key: str):
    batch, item = _new_login_item(session, key)
    attempt = TgAccountLoginBatchAttempt(
        item_id=item.id,
        batch_id=batch.id,
        tenant_id=1,
        execution_generation=1,
        phase="post_initialization_waiting",
    )
    session.add(attempt)
    session.flush()
    item.current_attempt_id = attempt.id
    item.status = "post_initialization_waiting"
    item.authorization_status = "confirmed"
    return batch, item


def test_succeeded_owner_reentry_creates_current_gap_decision(session_factory) -> None:
    with session_factory() as session:
        _, first_item = _new_login_item(session, "succeeded-owner-first")
        first = create_or_attach_full_initialization(session, first_item, actor="操作员")
        profile_batch = TgAccountSecurityBatch(tenant_id=1, status="completed")
        session.add(profile_batch)
        session.flush()
        profile_item = TgAccountSecurityBatchItem(
            batch_id=profile_batch.id,
            tenant_id=1,
            account_id=40,
            status="succeeded",
            profile_status="succeeded",
            avatar_status="succeeded",
            generated_display_name="林岚",
            avatar_source="material:9",
        )
        session.add(profile_item)
        session.flush()
        first.status = first.stage = "succeeded"
        first.two_fa_status = first.profile_status = first.abc_status = "succeeded"
        first.profile_batch_id = profile_batch.id
        first.profile_item_id = profile_item.id
        first.profile_target_name = "林岚"
        first.profile_target_avatar_source = "material:9"
        first.profile_target_avatar_object_key = "avatars/linlan.jpg"
        _, second_item = _new_login_item(session, "succeeded-owner-second")

        second = create_or_attach_full_initialization(session, second_item, actor="操作员")
        session.commit()

    assert second.id != first.id
    assert second.generation == first.generation + 1
    assert second.predecessor_initialization_id == first.id
    assert second.profile_target_name == "林岚"
    assert second.status == "waiting_login_parent"


@pytest.mark.parametrize("terminal_status", ["manual_required", "failed", "reconcile_unknown"])
def test_reentry_keeps_original_terminal_debt(session_factory, terminal_status: str) -> None:
    with session_factory() as session:
        _, first_item = _new_login_item(session, f"terminal-{terminal_status}-first")
        first = create_or_attach_full_initialization(session, first_item, actor="操作员")
        first.status = terminal_status
        first.stage = terminal_status
        first.failure_type = f"original_{terminal_status}"
        if terminal_status == "reconcile_unknown":
            first.two_fa_status = "reconcile_unknown"
        _, second_item = _new_login_item(session, f"terminal-{terminal_status}-second")

        second = create_or_attach_full_initialization(session, second_item, actor="操作员")
        session.commit()
        owners = list(session.scalars(select(TgAccountFullInitialization)))

    assert second.id == first.id
    assert len(owners) == 1
    assert second.failure_type == f"original_{terminal_status}"


def test_manual_post_init_finishes_batch_with_manual_status(session_factory) -> None:
    with session_factory() as session:
        batch, item = _terminal_item(session, "batch-manual-terminal")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = owner.stage = "manual_required"
        owner.failure_type = "two_fa_current_password_unavailable"
        sync_parent_bindings(session, owner)
        session.commit()
        session.refresh(batch)

    assert item.status == "failed"
    assert batch.status == "completed_with_manual"
    assert batch.manual_required_count == 1


def test_failed_post_init_finishes_batch_with_failures_status(session_factory) -> None:
    with session_factory() as session:
        batch, item = _terminal_item(session, "batch-failed-terminal")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = owner.stage = "failed"
        owner.failure_type = "profile_remote_mismatch"
        sync_parent_bindings(session, owner)
        session.commit()
        session.refresh(batch)

    assert item.status == "failed"
    assert batch.status == "completed_with_failures"


def test_cancelled_batch_gets_correction_when_post_init_changes(session_factory) -> None:
    with session_factory() as session:
        batch, item = _terminal_item(session, "cancelled-correction")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        batch.status = "cancelled"
        item.status = "skipped"
        item.post_initialization_status = "pending"
        finalize_batch_if_terminal(session, batch.id)
        session.commit()
        initial_count = len(list(session.scalars(select(TgAccountLoginBatchNotification))))
        owner.status = owner.stage = "succeeded"
        owner.two_fa_status = owner.profile_status = owner.abc_status = "succeeded"
        sync_parent_bindings(session, owner)
        session.commit()
        notifications = list(session.scalars(select(TgAccountLoginBatchNotification)))

    assert len(notifications) == initial_count + 2
    assert {row.event_type for row in notifications} == {"initial", "correction"}


def test_terminal_abc_manual_debt_does_not_create_second_request(session_factory) -> None:
    with session_factory() as session:
        _, first_item = _new_login_item(session, "abc-debt-first")
        first = create_or_attach_full_initialization(session, first_item, actor="操作员")
        first.status = first.stage = "manual_required"
        first.failure_type = "abc_manual_required"
        session.add(TgPostLoginAbcRequest(
            tenant_id=1,
            account_id=40,
            full_initialization_id=first.id,
            status="manual_required",
            requested_by="操作员",
        ))
        _, second_item = _new_login_item(session, "abc-debt-second")
        second = create_or_attach_full_initialization(session, second_item, actor="操作员")
        session.commit()
        requests = list(session.scalars(select(TgPostLoginAbcRequest)))

    assert second.id == first.id
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("actions", "expected"),
    [
        (("update_profile",), ["update_profile"]),
        (("update_avatar",), ["update_avatar"]),
    ],
)
def test_profile_payload_only_mutates_actual_gap(actions, expected) -> None:
    owner = TgAccountFullInitialization(
        id=9,
        tenant_id=1,
        account_id=40,
        target_pool_id=10,
        profile_target_name="林岚",
        profile_target_avatar_source="material:9",
    )

    payload = _profile_payload(40, actions=actions, owner=owner)

    assert payload.action_types == expected
    assert payload.profile_strategy.overwrite_existing is True
    assert len(payload.preview_overrides) == 1
    assert payload.preview_overrides[0].generated_display_name == "林岚"
    assert payload.preview_overrides[0].avatar_source == "material:9"


def test_first_profile_payload_generates_a_new_name_without_empty_override() -> None:
    owner = TgAccountFullInitialization(
        id=10,
        tenant_id=1,
        account_id=40,
        target_pool_id=10,
    )

    payload = _profile_payload(
        40,
        actions=("update_profile", "update_avatar"),
        owner=owner,
    )

    assert payload.preview_overrides == []


def test_full_init_requires_final_active_a_readback(session_factory, monkeypatch) -> None:
    from app.services.account_login import remote_phases

    class InactiveGateway:
        def check_account_health_isolated(self, *_args, **_kwargs):
            return type("Health", (), {"status": "异常", "detail": "not active"})()

    monkeypatch.setattr(remote_phases, "gateway", InactiveGateway())
    with session_factory() as session:
        batch, item = _new_login_item(session, "inactive-final-a")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        attempt = TgAccountLoginBatchAttempt(
            item_id=item.id,
            batch_id=batch.id,
            tenant_id=1,
            execution_generation=1,
            phase="online_readback",
            lease_token="online-readback-lease",
        )
        session.add(attempt)
        session.flush()
        item.current_attempt_id = attempt.id
        item.status = "running"
        item.phase = "online_readback"
        session.commit()
        claim = PhaseClaim(
            batch.id,
            item.id,
            attempt.id,
            1,
            1,
            "online_readback",
            "online-readback-lease",
        )

    remote_phases._online_readback(session_factory, claim, object())

    with session_factory() as session:
        item = session.get(type(item), item.id)
        owner = session.get(TgAccountFullInitialization, owner.id)

    assert item.status == "failed"
    assert item.failure_type == "primary_online_readback_unproven"
    assert owner.status == "failed"
    assert owner.failure_type == "primary_online_readback_unproven"


def test_reconcile_request_reopens_same_unknown_operation(session_factory) -> None:
    with session_factory() as session:
        _, item = _new_login_item(session, "unknown-reconcile-request")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = owner.stage = "reconcile_unknown"
        owner.two_fa_status = "reconcile_unknown"
        owner.two_fa_call_state = "unknown"
        owner.two_fa_request_key = "full-init:1:two-fa:1"
        owner.failure_type = "two_fa_remote_unknown"
        expected_version = owner.version
        session.commit()

        result = request_post_login_reconciliation(
            session,
            1,
            owner.id,
            expected_version=expected_version,
            actor="操作员",
            reason="重新核验远端结果",
        )

    assert result.status == "pending"
    assert result.stage == "reconcile"
    assert result.two_fa_request_key == "full-init:1:two-fa:1"


def test_two_fa_candidate_reopens_original_manual_owner(session_factory) -> None:
    with session_factory() as session:
        _, item = _new_login_item(session, "manual-candidate")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = owner.stage = "manual_required"
        owner.two_fa_status = "manual_required"
        owner.failure_type = "two_fa_current_password_unavailable"
        expected_version = owner.version
        session.commit()

        result = submit_two_fa_candidate(
            session,
            1,
            owner.id,
            expected_version=expected_version,
            actor="操作员",
            reason="提交当前2FA候选",
            candidate_password="candidate-password",
        )

    assert result.id == owner.id
    assert result.status == "pending"
    assert result.stage == "two_fa"
    assert result.source_two_fa_kind == "operator_candidate"
    assert decrypt_secret(result.source_two_fa_password_ciphertext) == "candidate-password"


def test_two_fa_reset_reopens_original_owner_without_candidate_secret(session_factory) -> None:
    with session_factory() as session:
        _, item = _new_login_item(session, "manual-reset")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = owner.stage = "manual_required"
        owner.two_fa_status = "manual_required"
        owner.failure_type = "two_fa_manual_required"
        owner.source_two_fa_kind = "operator_candidate"
        owner.source_two_fa_password_ciphertext = "encrypted-candidate"
        expected_version = owner.version
        session.commit()

        result = request_two_fa_reset(
            session,
            1,
            owner.id,
            expected_version=expected_version,
            actor="操作员",
            reason="当前密码已确认无效，发起官方重置",
        )

    assert result.id == owner.id
    assert result.status == "pending"
    assert result.stage == "two_fa"
    assert result.source_two_fa_kind == "telegram_reset_requested"
    assert result.source_two_fa_password_ciphertext == ""


def test_resumed_manual_batch_returns_to_running_until_correction(session_factory) -> None:
    with session_factory() as session:
        batch, item = _terminal_item(session, "batch-manual-resume")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = owner.stage = "manual_required"
        owner.failure_type = "two_fa_current_password_unavailable"
        sync_parent_bindings(session, owner)
        owner.status = "pending"
        owner.stage = "two_fa"
        owner.failure_type = ""
        sync_parent_bindings(session, owner)
        session.commit()
        session.refresh(batch)

    assert batch.status == "running"
    assert batch.post_init_waiting_count == 1


def test_terminal_manual_batch_cannot_be_relabelled_cancelled(session_factory) -> None:
    with session_factory() as session:
        batch, item = _terminal_item(session, "manual-terminal-cancel")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = owner.stage = "manual_required"
        owner.failure_type = "two_fa_current_password_unavailable"
        sync_parent_bindings(session, owner)
        session.commit()
        expected_version = batch.state_version

        result = cancel_login_batch(
            session,
            1,
            batch.id,
            expected_version,
            "操作员",
            "不应覆盖人工终态",
        )

    assert result.status == "completed_with_manual"


def test_post_init_failure_cannot_retry_whole_login(session_factory, monkeypatch) -> None:
    from app.services.account_login import batches

    monkeypatch.setattr(batches, "require_batch_login_enabled", lambda _session: None)
    with session_factory() as session:
        batch, item = _terminal_item(session, "post-init-whole-retry")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = owner.stage = "failed"
        owner.failure_type = "profile_remote_mismatch"
        sync_parent_bindings(session, owner)
        session.commit()
        payload = LoginBatchRetryRequest(
            item_ids=[item.id],
            expected_state_version=batch.state_version,
            reason="错误地重试整条登录",
        )

        with pytest.raises(BatchLoginError, match="完整初始化专项操作"):
            retry_login_batch_items(session, 1, batch.id, payload, "操作员")


def test_post_init_success_does_not_erase_pool_transition_failure(session_factory) -> None:
    with session_factory() as session:
        _, item = _new_login_item(session, "pool-failure-preserved")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        item.authorization_status = "confirmed"
        item.status = item.phase = "failed"
        item.failure_type = "pool_transition_failed"
        item.failure_detail = "目标分组迁移失败"
        owner.status = owner.stage = "succeeded"
        owner.two_fa_status = owner.profile_status = owner.abc_status = "succeeded"

        sync_parent_bindings(session, owner)
        session.commit()

    assert item.status == "failed"
    assert item.failure_type == "pool_transition_failed"
    assert item.post_initialization_status == "succeeded"
    assert item.post_initialization_failure_type == ""


def test_two_fa_reconcile_exception_remains_unknown(session_factory, monkeypatch) -> None:
    from app.services.account_post_login_init import reconcile

    class FailingGateway:
        def get_two_fa_status(self, *_args):
            raise TimeoutError("remote readback unavailable")

    monkeypatch.setattr(reconcile, "gateway", FailingGateway())
    with session_factory() as session:
        _, item = _new_login_item(session, "reconcile-exception")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = "running"
        owner.stage = "reconcile"
        owner.two_fa_status = "reconcile_unknown"
        owner.lease_token = "reconcile-exception-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, owner.stage, owner.lease_token)

    execute_reconcile_stage(session_factory, claim)

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, owner.id)

    assert owner.status == "reconcile_unknown"
    assert owner.failure_type == "two_fa_remote_unknown"
    assert owner.failure_detail == "TimeoutError"
