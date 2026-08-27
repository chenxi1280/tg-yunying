from __future__ import annotations

import pytest
from sqlalchemy import select

from app.integrations.telegram.contracts import RemoteAvatarFingerprint, RemoteProfile
from app.models import (
    TgAccount,
    TgAccountAuthorization,
    TgAccountFullInitialization,
    TgAccountProfileNameClaim,
    TgAccountSecurityBatch,
    TgAccountSecurityBatchItem,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgPostLoginAbcRequest,
)
from app.security import encrypt_session
from app.services.account_post_login_init.abc import (
    approve_post_login_abc_request,
    execute_abc_stage,
)
from app.services.account_post_login_init.binding import create_or_attach_full_initialization
from app.services.account_post_login_init.contracts import FullInitializationClaim
from app.services.account_post_login_init.profile import execute_profile_stage
from tests.test_account_post_login_full_init import _new_login_item, session_factory


pytestmark = pytest.mark.no_postgres


def test_profile_batch_persists_owner_idempotency_key(session_factory) -> None:
    with session_factory() as session:
        _, item = _new_login_item(session, "profile-idempotency")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = "running"
        owner.stage = "profile"
        owner.two_fa_status = "succeeded"
        owner.lease_token = "profile-idempotency-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, "profile", owner.lease_token)

    execute_profile_stage(session_factory, claim)

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)
        batch = session.get(TgAccountSecurityBatch, owner.profile_batch_id)

    assert batch.idempotency_key == f"post-login-profile:{owner.id}"


def test_abc_stage_creates_one_separate_approval_request(session_factory) -> None:
    with session_factory() as session:
        _, item = _new_login_item(session, "abc-request")
        owner = create_or_attach_full_initialization(session, item, actor="原操作员")
        owner.status = "running"
        owner.stage = "abc"
        owner.two_fa_status = "succeeded"
        owner.two_fa_evidence_ref = "two-fa-evidence"
        owner.profile_status = "succeeded"
        owner.profile_evidence_ref = "profile-evidence"
        owner.lease_token = "abc-lease-one"
        session.commit()
        claim = FullInitializationClaim(owner.id, "abc", owner.lease_token)

    execute_abc_stage(session_factory, claim)

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)
        owner.status = "running"
        owner.lease_token = "abc-lease-two"
        session.commit()
    execute_abc_stage(
        session_factory,
        FullInitializationClaim(owner.id, "abc", owner.lease_token),
    )

    with session_factory() as session:
        requests = list(session.scalars(select(TgPostLoginAbcRequest)))
        owner = session.get(TgAccountFullInitialization, owner.id)

    assert len(requests) == 1
    assert requests[0].requested_by == "操作员"
    assert owner.status == "waiting_abc_approval"


def test_abc_stage_rejects_slot_only_false_positive(session_factory) -> None:
    with session_factory() as session:
        _, item = _new_login_item(session, "abc-false-positive")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        account = session.get(TgAccount, 40)
        primary = session.get(TgAccountAuthorization, account.current_authorization_id)
        session.add_all(_false_positive_abc_rows(account, primary))
        owner.status = "running"
        owner.stage = "abc"
        owner.two_fa_status = "succeeded"
        owner.two_fa_evidence_ref = "two-fa-evidence"
        owner.profile_status = "succeeded"
        owner.profile_evidence_ref = "profile-evidence"
        owner.lease_token = "abc-false-positive-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, "abc", owner.lease_token)

    execute_abc_stage(session_factory, claim)

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)
        requests = list(session.scalars(select(TgPostLoginAbcRequest)))

    assert owner.status == "waiting_abc_approval"
    assert owner.abc_evidence_ref == ""
    assert len(requests) == 1


def _false_positive_abc_rows(account, primary) -> list:
    return [
        TgAccountAuthorization(
            tenant_id=1, account_id=40, role="standby_1", logical_slot="standby_1",
            provision_region_code="sv", developer_app_id=30,
            session_ciphertext=encrypt_session("standby-b"), status="active",
            health_status="healthy", is_slot_current=True,
        ),
        TgAccountAuthorization(
            tenant_id=1, account_id=40, role="standby_2", logical_slot="standby_2",
            provision_region_code="my", developer_app_id=30, status="active",
            health_status="healthy", is_slot_current=True,
        ),
        TgAuthorizationDrOperation(
            tenant_id=1, account_id=40, operation_type="abc_e4_primary_send",
            logical_slot="primary", source_generation=1, target_generation=1,
            developer_app_id=30, developer_app_api_id_snapshot=10030,
            developer_app_credentials_version=1, assignment_version=1,
            egress_id="primary_regular:direct", egress_version=1,
            idempotency_key="false-positive-e4", request_fingerprint="f" * 64,
            status="succeeded", remote_call_state="succeeded",
            source_authorization_id=primary.id, code_source_authorization_id=primary.id,
            expected_current_authorization_id=account.current_authorization_id,
            expected_authorization_generation=account.authorization_generation,
            expected_authorization_fact_generation=account.authorization_fact_generation,
            expected_connection_generation=account.connection_generation,
            expected_code_source_fact_version=primary.fact_version,
            requested_by="操作员", approved_by="批准人", approval_ref="FALSE-POSITIVE",
        ),
    ]


def test_abc_approval_records_manifest_batch_id(session_factory, monkeypatch) -> None:
    from app.services.account_post_login_init import abc

    with session_factory() as session:
        _, item = _new_login_item(session, "abc-approve")
        owner = create_or_attach_full_initialization(session, item, actor="原操作员")
        owner.status = "running"
        owner.stage = "abc"
        owner.two_fa_status = "succeeded"
        owner.two_fa_evidence_ref = "two-fa-evidence"
        owner.profile_status = "succeeded"
        owner.profile_evidence_ref = "profile-evidence"
        owner.lease_token = "abc-approval-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, "abc", owner.lease_token)
    execute_abc_stage(session_factory, claim)
    monkeypatch.setattr(
        abc,
        "apply_post_login_online_abc_batch",
        lambda *_args, **_kwargs: {"batch_id": "post-login-abc-batch"},
    )

    with session_factory() as session:
        request = session.scalar(select(TgPostLoginAbcRequest))
        result = approve_post_login_abc_request(
            session,
            1,
            request.id,
            expected_version=request.request_version,
            deployed_release_sha="a" * 40,
            expected_fingerprint="b" * 64,
            approved_by="批准人",
            approval_ref="ABC-APPROVAL",
        )

    assert result["status"] == "approved"
    assert result["abc_batch_id"] == "post-login-abc-batch"


def test_succeeded_abc_item_without_complete_evidence_stays_unknown(session_factory) -> None:
    with session_factory() as session:
        _, login_item = _new_login_item(session, "abc-item-missing-evidence")
        owner = create_or_attach_full_initialization(session, login_item, actor="操作员")
        batch = TgAuthorizationOnlineAbcBatch(
            tenant_id=1, idempotency_key="abc-item-missing-evidence",
            target_set_fingerprint="a" * 64, target_count=1,
            deployed_release_sha="b" * 40, selection_mode="post_login_exact",
            status="completed", requested_by="操作员", approved_by="批准人",
        )
        session.add(batch)
        session.flush()
        item = TgAuthorizationOnlineAbcItem(
            batch_id=batch.id, tenant_id=1, account_id=40, ordinal=1,
            primary_fact_version=1, authorization_generation=0,
            authorization_fact_generation=0, connection_generation=0,
            primary_session_digest="c" * 64, app_b_credentials_version=1,
            app_b_assignment_purpose="standby_1", app_b_assignment_version=1,
            source_c_fact_version=0, source_c_slot_generation=0,
            status="succeeded", outcome="succeeded",
        )
        session.add(item)
        owner.status = "running"
        owner.stage = "abc"
        owner.two_fa_status = "succeeded"
        owner.profile_status = "succeeded"
        owner.abc_batch_id = batch.id
        owner.lease_token = "abc-item-missing-evidence-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, "abc", owner.lease_token)

    execute_abc_stage(session_factory, claim)

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)

    assert owner.status == "reconcile_unknown"
    assert owner.failure_detail == "abc_success_evidence_unproven"


class _ProfileReadbackGateway:
    def pull_profile(self, _account_id, _session, _credentials):
        return RemoteProfile(first_name="林岚", last_name="", bio="")

    def pull_profile_avatar_fingerprint(self, _account_id, **_kwargs):
        return RemoteAvatarFingerprint(
            sha256="remote-sha",
            size_bytes=1024,
            remote_photo_id="remote-photo",
            perceptual_hash="0" * 16,
        )


def test_profile_stage_requires_platform_and_telegram_readback(session_factory, monkeypatch) -> None:
    from app.services.account_post_login_init import profile

    monkeypatch.setattr(profile, "gateway", _ProfileReadbackGateway())
    monkeypatch.setattr(
        profile,
        "_local_avatar_fingerprint",
        lambda _key: {"sha256": "local-sha", "perceptual_hash": "0" * 16},
    )
    with session_factory() as session:
        _, login_item = _new_login_item(session, "profile-readback")
        owner = create_or_attach_full_initialization(session, login_item, actor="操作员")
        account = session.get(TgAccount, 40)
        account.display_name = "林岚"
        account.tg_first_name = "林岚"
        account.tg_last_name = ""
        account.avatar_object_key = "avatars/linlan.jpg"
        security_batch = TgAccountSecurityBatch(
            tenant_id=1, action_types='["update_profile","update_avatar"]',
            status="completed", total_count=1, success_count=1,
        )
        session.add(security_batch)
        session.flush()
        security_item = TgAccountSecurityBatchItem(
            batch_id=security_batch.id, tenant_id=1, account_id=40,
            status="succeeded", profile_status="succeeded", avatar_status="succeeded",
            generated_display_name="林岚",
        )
        session.add(security_item)
        session.flush()
        session.add(TgAccountProfileNameClaim(
            tenant_id=1, account_id=40, display_name="林岚", name_key="林岚",
            batch_id=security_batch.id, batch_item_id=security_item.id,
        ))
        owner.profile_batch_id = security_batch.id
        owner.status = "running"
        owner.stage = "profile"
        owner.profile_status = "running"
        owner.lease_token = "profile-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, "profile", owner.lease_token)

    execute_profile_stage(session_factory, claim)

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)

    assert owner.status == "pending"
    assert owner.stage == "abc"
    assert owner.profile_status == "succeeded"
    assert owner.profile_evidence_ref == f"full-init:{owner.id}:profile"
