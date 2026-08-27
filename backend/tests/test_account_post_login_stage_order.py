from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import (
    TgAccountFullInitialization,
    TgAccountLoginBatchItem,
    TgAccountSecuritySnapshot,
    TgPostLoginAbcRequest,
)
from app.security import decrypt_secret
from app.services._common import _now
from app.services.account_post_login_init.abc import execute_abc_stage
from app.services.account_post_login_init.binding import create_or_attach_full_initialization
from app.services.account_post_login_init.contracts import FullInitializationClaim
from app.services.account_post_login_init.flow import advance_full_initialization
from app.services.account_post_login_init.two_fa import record_two_fa_reset_waiting
from app.services.account_post_login_init.two_fa import record_two_fa_eligibility_waiting
from tests.test_account_post_login_full_init import _new_login_item, session_factory


pytestmark = pytest.mark.no_postgres


def test_new_full_initialization_starts_with_profile(session_factory) -> None:
    with session_factory() as session:
        _, item = _new_login_item(session, "profile-first-owner")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")

    assert owner.stage == "profile"
    assert owner.profile_status == "pending"
    assert owner.two_fa_status == "pending"


def test_reset_waiting_preserves_server_due_and_runs_profile(session_factory) -> None:
    due_at = _now() + timedelta(days=7)
    with session_factory() as session:
        _, item = _new_login_item(session, "reset-wait-profile-first")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = "running"
        owner.stage = "two_fa"
        owner.lease_token = "reset-wait-profile-lease"
        record_two_fa_reset_waiting(session, owner, due_at)
        session.commit()

    assert owner.two_fa_status == "reset_waiting"
    assert owner.two_fa_next_retry_at == due_at
    assert owner.stage == "profile"
    assert owner.status == "pending"
    assert owner.next_retry_at is None


def test_reset_eligibility_wait_also_runs_profile_before_retry(session_factory) -> None:
    due_at = _now() + timedelta(hours=24)
    with session_factory() as session:
        _, item = _new_login_item(session, "reset-eligibility-profile-first")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = "running"
        owner.stage = "two_fa"
        owner.lease_token = "eligibility-wait-lease"
        record_two_fa_eligibility_waiting(session, owner, due_at)

    assert owner.source_two_fa_kind == "telegram_reset_eligibility_waiting"
    assert owner.two_fa_status == "reset_eligibility_waiting"
    assert owner.two_fa_next_retry_at == due_at
    assert owner.stage == "profile"
    assert owner.next_retry_at is None


def test_abc_request_is_prepared_without_starting_b_before_fixed_two_fa(
    session_factory,
) -> None:
    due_at = _now() + timedelta(days=7)
    with session_factory() as session:
        _, item = _new_login_item(session, "abc-prerequisite-request")
        owner = create_or_attach_full_initialization(session, item, actor="原操作员")
        owner.status = "running"
        owner.stage = "abc"
        owner.profile_status = "succeeded"
        owner.profile_evidence_ref = "profile-evidence"
        owner.two_fa_status = "reset_waiting"
        owner.two_fa_next_retry_at = due_at
        owner.lease_token = "abc-prerequisite-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, "abc", owner.lease_token)

    execute_abc_stage(session_factory, claim)

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)
        request = session.scalar(select(TgPostLoginAbcRequest))

    assert request.status == "waiting_prerequisite"
    assert request.abc_batch_id == ""
    assert owner.abc_status == "waiting_prerequisite"
    assert owner.stage == "two_fa"
    assert owner.status == "pending"
    assert owner.next_retry_at == due_at


def test_telegram_accepted_password_allows_abc_before_fixed_two_fa(
    session_factory,
) -> None:
    with session_factory() as session:
        _, item = _new_login_item(session, "abc-before-fixed-two-fa")
        owner = create_or_attach_full_initialization(
            session,
            item,
            actor="原操作员",
            source_two_fa_kind="telegram_accepted",
            source_two_fa_password="accepted-password",
        )
        owner.status = "running"
        owner.stage = "abc"
        owner.profile_status = "succeeded"
        owner.profile_evidence_ref = "profile-evidence"
        owner.lease_token = "abc-before-fixed-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, "abc", owner.lease_token)

    execute_abc_stage(session_factory, claim)

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)
        snapshot = session.scalar(select(TgAccountSecuritySnapshot))
        request = session.scalar(select(TgPostLoginAbcRequest))

    assert owner.two_fa_status == "pending"
    assert owner.two_fa_evidence_ref == ""
    assert owner.status == "waiting_abc_approval"
    assert request.status == "waiting_approval"
    assert snapshot.two_fa_password_source == "telegram_accepted_import"
    assert decrypt_secret(snapshot.two_fa_password_ciphertext) == "accepted-password"


def test_missing_two_fa_allows_abc_before_platform_sets_fixed_password(
    session_factory,
) -> None:
    with session_factory() as session:
        _, item = _new_login_item(session, "abc-before-new-fixed-two-fa")
        owner = create_or_attach_full_initialization(
            session,
            item,
            actor="原操作员",
            source_two_fa_kind="telegram_missing",
        )
        owner.status = "running"
        owner.stage = "abc"
        owner.profile_status = "succeeded"
        owner.profile_evidence_ref = "profile-evidence"
        owner.lease_token = "abc-before-new-fixed-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, "abc", owner.lease_token)

    execute_abc_stage(session_factory, claim)

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)
        request = session.scalar(select(TgPostLoginAbcRequest))

    assert owner.status == "waiting_abc_approval"
    assert owner.two_fa_status == "pending"
    assert request.status == "waiting_approval"


def test_completed_abc_advances_to_fixed_two_fa_last(session_factory) -> None:
    with session_factory() as session:
        _, item = _new_login_item(session, "abc-complete-before-fixed")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.profile_status = "succeeded"
        owner.profile_evidence_ref = "profile-evidence"
        owner.abc_status = "succeeded"
        owner.abc_evidence_ref = "abc-evidence"
        advance_full_initialization(owner)

    assert owner.status == "pending"
    assert owner.stage == "two_fa"
    assert owner.two_fa_status == "pending"


def test_existing_prerequisite_request_wakes_when_import_password_is_recorded(
    session_factory,
) -> None:
    with session_factory() as session:
        _, item = _new_login_item(session, "abc-request-wake-before-fixed")
        owner = create_or_attach_full_initialization(session, item, actor="原操作员")
        owner.status = "running"
        owner.stage = "abc"
        owner.profile_status = "succeeded"
        owner.profile_evidence_ref = "profile-evidence"
        owner.lease_token = "abc-create-prerequisite-lease"
        session.commit()
        item_id = item.id
        first_claim = FullInitializationClaim(owner.id, "abc", owner.lease_token)
    execute_abc_stage(session_factory, first_claim)

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, first_claim.initialization_id)
        item = session.get(TgAccountLoginBatchItem, item_id)
        owner = create_or_attach_full_initialization(
            session,
            item,
            actor="原操作员",
            source_two_fa_kind="telegram_accepted",
            source_two_fa_password="accepted-password",
        )
        owner.status = "running"
        owner.stage = "abc"
        owner.lease_token = "abc-wake-prerequisite-lease"
        session.commit()
        second_claim = FullInitializationClaim(owner.id, "abc", owner.lease_token)
    execute_abc_stage(session_factory, second_claim)

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, second_claim.initialization_id)
        requests = list(session.scalars(select(TgPostLoginAbcRequest)))

    assert len(requests) == 1
    assert requests[0].status == "waiting_approval"
    assert owner.status == "waiting_abc_approval"


def test_fixed_two_fa_wakes_prepared_abc_request(session_factory) -> None:
    with session_factory() as session:
        _, item = _new_login_item(session, "fixed-wakes-abc")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.profile_status = "succeeded"
        owner.profile_evidence_ref = "profile-evidence"
        owner.abc_status = "waiting_prerequisite"
        owner.two_fa_status = "succeeded"
        owner.two_fa_evidence_ref = "two-fa-evidence"
        advance_full_initialization(owner)

    assert owner.stage == "abc"
    assert owner.status == "pending"


def test_owner_finishes_only_after_profile_two_fa_and_abc_evidence(
    session_factory,
) -> None:
    with session_factory() as session:
        _, item = _new_login_item(session, "all-evidence-final")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.profile_status = "succeeded"
        owner.profile_evidence_ref = "profile-evidence"
        owner.two_fa_status = "succeeded"
        owner.two_fa_evidence_ref = "two-fa-evidence"
        owner.abc_status = "succeeded"
        owner.abc_evidence_ref = "abc-evidence"
        advance_full_initialization(owner)

    assert owner.status == "succeeded"
    assert owner.stage == "succeeded"
    assert owner.finished_at is not None
