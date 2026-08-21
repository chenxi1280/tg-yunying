from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram.contracts import AuthorizationIdentity
from app.models import (
    AccountProxy,
    AccountStatus,
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationDrReconcileCase,
    TgLoginFlow,
)
from app.services._common import _now
from app.services.account_authorizations import _mark_same_role_for_repair
from app.services.authorization_dr.sv_login_recovery import (
    apply_sv_login_recovery,
    preview_sv_login_recovery,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(session: Session):
    session.add(Tenant(id=1, name="SV recovery"))
    session.add_all([
        TelegramDeveloperApp(id=1, app_name="App A", api_id=1001, api_hash_ciphertext="a"),
        TelegramDeveloperApp(id=2, app_name="App B", api_id=1002, api_hash_ciphertext="b"),
    ])
    session.add(AccountProxy(
        id=8, tenant_id=1, name="sv", host="127.0.0.1", port=1080, status="healthy",
    ))
    account = TgAccount(
        id=101, tenant_id=1, display_name="abc", phone_masked="101",
        session_ciphertext="a-session", developer_app_id=2, proxy_id=8,
        authorization_generation=4, authorization_fact_generation=5, connection_generation=6,
    )
    session.add(account)
    session.flush()
    primary = TgAccountAuthorization(
        tenant_id=1, account_id=101, role="primary", logical_slot="primary",
        provision_region_code="sv", developer_app_id=2, proxy_id=8,
        session_ciphertext="a-session", status="active", health_status="healthy",
        is_current=True, is_slot_current=True, telegram_user_id_digest="1" * 64,
        auth_key_fingerprint_digest="2" * 64, fact_version=3,
    )
    conflict = TgAccountAuthorization(
        tenant_id=1, account_id=101, role="standby_1", logical_slot="standby_1",
        provision_region_code="sv", developer_app_id=2, proxy_id=8,
        session_ciphertext="old-b", status="standby", health_status="healthy",
        is_slot_current=True, protected_from_cleanup=True, fact_version=2,
    )
    session.add_all([primary, conflict])
    session.flush()
    account.current_authorization_id = primary.id
    flow = TgLoginFlow(
        tenant_id=1, account_id=101, method="code", status=AccountStatus.WAITING_CODE.value,
        authorization_role="standby_1", developer_app_id=1, proxy_id=8,
        temporary_session_ciphertext="temp", phone_code_hash_ciphertext="phone-hash",
        challenge_sent_at=_now(),
    )
    session.add(flow)
    session.flush()
    operation = TgAuthorizationDrOperation(
        tenant_id=1, account_id=101, operation_type="provision_standby_1",
        logical_slot="standby_1", source_authorization_id=primary.id,
        code_source_authorization_id=primary.id, source_generation=1, target_generation=1,
        expected_current_authorization_id=primary.id, expected_authorization_generation=4,
        expected_authorization_fact_generation=5, expected_connection_generation=6,
        expected_code_source_fact_version=3, expected_code_source_user_id_digest="1" * 64,
        expected_code_source_auth_key_digest="2" * 64, developer_app_id=1,
        developer_app_api_id_snapshot=1001, developer_app_credentials_version=1,
        assignment_version=1, egress_id="sv-proxy:8", egress_version=1,
        idempotency_key="abc-101", request_fingerprint="f" * 64,
        status="reconcile_unknown", blocker_code="IntegrityError", remote_call_state="unknown",
        login_flow_id=flow.id, requested_by="requester", approved_by="reviewer", approval_ref="approved",
    )
    session.add(operation)
    session.add(AuthorizationDrRuntimeContract(id=1, mode="off"))
    session.add(AuthorizationDrExecutionNode(
        id="my-node-1", region_code="my", purpose="standby_session_dr",
        capability_version="2.21", runtime_image_sha="m" * 40,
        standby_egress_id="my", status="ready", active_client_count=0,
    ))
    session.commit()
    return account, primary, conflict, flow, operation


def _mock_remote(monkeypatch) -> None:
    identity = AuthorizationIdentity(
        authorization_hash="987654", auth_key_fingerprint_digest="3" * 64,
        telegram_user_id_digest="1" * 64, authorization_fingerprint_digest="4" * 64,
    )
    remote = SimpleNamespace(
        authorization_hash="987654", is_current=False, api_id=1001,
        device_model="recovered", platform="test", date_created=_now(), date_active=_now(),
    )
    monkeypatch.setattr("app.services.authorization_dr.sv_login_recovery.decrypt_secret", lambda _: "raw-b")
    monkeypatch.setattr("app.services.authorization_dr.sv_login_recovery.encrypt_session", lambda x: f"session:{x}")
    monkeypatch.setattr("app.services.authorization_dr.sv_login_recovery.encrypt_secret", lambda x: f"secret:{x}")
    monkeypatch.setattr(
        "app.services.authorization_dr.sv_login_recovery.credentials_for_developer_app",
        lambda *_: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "app.services.authorization_dr.sv_login_recovery.credentials_for_authorization",
        lambda *_: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "app.services.authorization_dr.sv_login_recovery.gateway.authorization_identity",
        lambda *_: identity,
    )
    monkeypatch.setattr(
        "app.services.authorization_dr.sv_login_recovery.gateway.list_authorizations",
        lambda *_: [remote],
    )


def test_same_role_relogin_frees_current_slot_before_insert() -> None:
    with _session() as session:
        account, _primary, conflict, flow, _operation = _seed(session)
        _mark_same_role_for_repair(session, account, flow)
        replacement = TgAccountAuthorization(
            tenant_id=1, account_id=101, role="standby_1", logical_slot="standby_1",
            is_slot_current=True, developer_app_id=1,
        )
        session.add(replacement)
        session.flush()
        assert conflict.is_slot_current is False


def test_recovery_commits_existing_authorized_session_without_changing_a(monkeypatch) -> None:
    with _session() as session:
        account, primary, conflict, flow, operation = _seed(session)
        _mock_remote(monkeypatch)
        before = (account.current_authorization_id, account.session_ciphertext, account.developer_app_id,
                  account.authorization_generation, account.authorization_fact_generation,
                  account.connection_generation, primary.fact_version)
        preview = preview_sv_login_recovery(
            session, operation.id, tenant_id=1, runtime_image_sha="a" * 40, requested_by="requester",
        )
        result = apply_sv_login_recovery(
            session, operation.id, tenant_id=1, runtime_image_sha="a" * 40,
            requested_by="requester", actor="reviewer", approval_ref="USER-RECOVERY",
            idempotency_key="recover-101", expected_fingerprint=preview["evidence_fingerprint"],
        )

        session.refresh(account)
        session.refresh(primary)
        session.refresh(conflict)
        session.refresh(flow)
        after = (account.current_authorization_id, account.session_ciphertext, account.developer_app_id,
                 account.authorization_generation, account.authorization_fact_generation,
                 account.connection_generation, primary.fact_version)
        asset = session.get(TgAccountAuthorization, result["candidate_authorization_id"])
        case = session.scalar(select(TgAuthorizationDrReconcileCase).where(
            TgAuthorizationDrReconcileCase.operation_id == operation.id,
        ))
        assert after == before
        assert (asset.developer_app_id, asset.logical_slot, asset.is_slot_current) == (1, "standby_1", True)
        assert asset.session_ciphertext == "session:raw-b"
        assert (conflict.logical_slot, conflict.is_slot_current, conflict.protected_from_cleanup) == (
            "standby_repair", False, True,
        )
        assert flow.temporary_session_ciphertext is None
        assert (operation.status, operation.candidate_authorization_id) == ("succeeded", asset.id)
        assert (case.status, case.classification) == ("applied", "sv_login_session_recovered")
