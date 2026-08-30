from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

pytestmark = pytest.mark.no_postgres

from app.auth import create_admin_access_token
from app.database import Base, get_session
from app.main import app
from app.models import (
    OperationTarget,
    Action,
    RuleSet,
    RuleSetVersion,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TelegramDeveloperApp,
    TgGroup,
    TgGroupAccount,
    Task,
)
from app.models.enums import AccountStatus
from app.models.group_clone import CloneAccountSlot, CloneDeliveryObligation, CloneMessagePart, CloneSequencerHeadCase, CloneSourceEvent, CloneSourceStreamState, TelegramGatewayMutationIdentity
from app.integrations.telegram import SendResult
from app.integrations.telegram.update_contracts import (
    TelegramDifferenceBatch,
    TelegramNormalizedUpdate,
    TelegramOutboundMessageMapping,
)
from app.models.telegram_updates import (
    TelegramAuthorizationUpdateState,
    TelegramAuthorizationUpdateSubscription,
    TelegramOutboundRandomIdMapping,
)
from app.models.telegram_authorities import TelegramAuthorizationTransportState
from app.security import encrypt_secret
from app.services._common import _now
from app.services.task_center.executors.group_clone import build_plan as build_clone_plan
from app.services.task_center.dispatcher import dispatch_action
from app.services.task_center.group_clone_dispatch import validate_clone_dispatch
from app.services.task_center.payloads import GroupCloneSendPayload
from app.services.task_center.telegram_update_collector import drain_telegram_update_collector


@pytest.fixture
def client_and_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    tenant = Tenant(id=1, name="Tenant 1")
    session.add(tenant)
    session.flush()

    session.add(TelegramDeveloperApp(
        id=51,
        app_name="clone-test",
        api_id=12345,
        api_hash_ciphertext=encrypt_secret("test-api-hash"),
        is_active=True,
        health_status="健康",
    ))
    session.flush()

    for acc_id in [101, 102, 103]:
        acc = TgAccount(id=acc_id, tenant_id=1, display_name=f"Acc {acc_id}", phone_masked=f"+12345{acc_id}", status=AccountStatus.ACTIVE.value, developer_app_id=51, developer_app_version=1)
        session.add(acc)
    session.flush()
    for acc_id in [101, 102, 103]:
        authorization = TgAccountAuthorization(
            id=acc_id + 100,
            tenant_id=1,
            account_id=acc_id,
            is_current=True,
            status="active",
            slot_generation=1,
            developer_app_id=51,
            developer_app_api_id_snapshot=12345,
            session_ciphertext=f"session-{acc_id}",
            telegram_user_id_digest=f"digest-{acc_id}",
        )
        session.add(authorization)
        session.flush()
        session.add(TelegramAuthorizationUpdateState(
            tenant_id=1,
            account_id=acc_id,
            authorization_id=authorization.id,
            session_generation=1,
            state="live",
            owner_id=f"collector-{acc_id}",
            lease_expires_at=_now() + timedelta(minutes=10),
        ))
    session.add_all([
        OperationTarget(id=11, tenant_id=1, tg_peer_id="-100111", title="Source"),
        OperationTarget(id=12, tenant_id=1, tg_peer_id="-100222", title="Target"),
        TgGroup(id=21, tenant_id=1, tg_peer_id="-100111", title="Source"),
        TgGroup(id=22, tenant_id=1, tg_peer_id="-100222", title="Target"),
    ])
    session.flush()
    session.add_all([
        TgGroupAccount(id=1, tenant_id=1, group_id=21, account_id=101, is_listener=True),
        TgGroupAccount(id=2, tenant_id=1, group_id=22, account_id=101, can_send=True),
        TgGroupAccount(id=3, tenant_id=1, group_id=22, account_id=102, can_send=True, permission_label="管理员"),
        TgGroupAccount(id=4, tenant_id=1, group_id=22, account_id=103, can_send=True),
        RuleSet(id=31, tenant_id=1, name="clone-default", task_types=["group_clone"]),
        RuleSetVersion(id=32, tenant_id=1, rule_set_id=31, version=1, status="published"),
    ])
    session.commit()

    def override_get_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_session] = override_get_db
    client = TestClient(app)

    yield client, session

    app.dependency_overrides.clear()
    session.close()


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {create_admin_access_token()}"}


def test_api_precheck_and_create_start(client_and_session, monkeypatch):
    client, session = client_and_session
    headers = _auth_headers()

    payload = {
        "name": "Clone Task 1",
        "priority": 3,
        "timezone": "Asia/Shanghai",
        "source": {
            "internal_group_id": 21,
            "operation_target_id": 11,
            "peer_type": "channel",
            "peer_id": "-100111",
            "listener_account_id": 101,
            "authorization_id": 201,
            "authorization_mode": "admin_authorized",
        },
        "target": {
            "internal_group_id": 22,
            "operation_target_id": 12,
            "peer_type": "channel",
            "peer_id": "-100222",
            "control_account_id": 102,
            "control_authorization_id": 202,
        },
        "sender_pool": {
            "account_ids": [101, 102, 103],
        },
        "pacing": {
            "min_delay_ms": 2000,
            "max_delay_ms": 5000,
            "strict_target_order": True,
        },
        "content": {
            "rule_set_id": 31,
            "rule_set_version": 1,
        },
        "lifecycle": {
            "start_mode": "start_from_now",
            "failure_order_policy": "fail_stop",
        },
    }

    # 1. Precheck
    res = client.post("/api/tasks/group-clone/precheck", json=payload, headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["passed"] is True, data
    assert len(data["hard_blocks"]) == 0
    sender_ingress = session.scalar(select(TelegramAuthorizationUpdateState).where(
        TelegramAuthorizationUpdateState.authorization_id == 203,
    ))
    sender_ingress.lease_expires_at = _now() - timedelta(seconds=1)
    session.flush()
    blocked_precheck = client.post("/api/tasks/group-clone/precheck", json=payload, headers=headers)
    assert blocked_precheck.status_code == 200
    assert blocked_precheck.json()["passed"] is False
    assert any("owner/lease" in item for item in blocked_precheck.json()["hard_blocks"])
    sender_ingress.lease_expires_at = _now() + timedelta(minutes=10)
    session.flush()

    # 2. Create and start
    res_create = client.post("/api/tasks/group-clone/create-and-start", json=payload, headers=headers)
    assert res_create.status_code == 201
    create_data = res_create.json()
    assert create_data["success"] is True
    assert create_data["status"] == "pending"
    assert create_data["clone_start_state"] == "starting"
    task_id = create_data["task_id"]
    assert session.scalar(select(CloneSourceStreamState).where(CloneSourceStreamState.task_id == task_id)) is not None
    assert session.scalar(select(TelegramAuthorizationUpdateSubscription).where(TelegramAuthorizationUpdateSubscription.task_id == task_id)) is not None
    assert len(session.scalars(select(CloneAccountSlot).where(CloneAccountSlot.task_id == task_id)).all()) == 3

    # 3. Patch task config
    payload["pacing"]["min_delay_ms"] = 4000
    patch_payload = {
        "sender_pool": payload["sender_pool"],
        "pacing": payload["pacing"],
        "content": payload["content"],
        "lifecycle": payload["lifecycle"],
        "retention": {"source_event_days": 30, "media_cache_ttl_seconds": 86400},
    }
    res_patch = client.patch(f"/api/tasks/{task_id}/group-clone", json=patch_payload, headers=headers)
    assert res_patch.status_code == 200
    assert res_patch.json()["config_revision"] == 2
    unsafe_pool_patch = {**patch_payload, "sender_pool": {"account_ids": [101, 102]}}
    rejected_pool = client.patch(
        f"/api/tasks/{task_id}/group-clone",
        json=unsafe_pool_patch,
        headers=headers,
    )
    assert rejected_pool.status_code == 400
    assert "受控账号槽交接" in rejected_pool.json()["detail"]

    task = session.get(Task, task_id)
    stream = session.scalar(select(CloneSourceStreamState).where(CloneSourceStreamState.task_id == task_id))
    task.status = "running"
    task.stats = {**dict(task.stats or {}), "clone_start_state": "running"}
    stream.state = "live"
    session.add(CloneSourceEvent(
        tenant_id=1,
        task_id=task_id,
        task_lifecycle_epoch=1,
        source_peer_type="channel",
        source_peer_id="-100111",
        source_message_id=501,
        event_type="message_new",
        event_identity_hash="event-501",
        apply_order_key="0001",
        stream_order_no=1,
        sender_peer_type="user",
        sender_peer_id="source-user-501",
        media_type="text",
        content="clone message",
        entities=[],
        content_fingerprint="content-501",
        config_revision=2,
    ))
    session.flush()
    assert build_clone_plan(session, task) == 1
    action = session.scalar(select(Action).where(Action.task_id == task_id, Action.action_type == "group_clone_send"))
    identity = session.scalar(select(TelegramGatewayMutationIdentity).where(TelegramGatewayMutationIdentity.task_id == task_id))
    assert action is not None and action.payload["random_id"] == identity.random_id
    assert identity.account_id in {101, 102, 103}

    transport_block = TelegramAuthorizationTransportState(
        tenant_id=1,
        authorization_id=identity.authorization_id,
        session_generation=identity.session_generation,
        scope_type="global",
        target_peer_key="*",
        blocked_until=_now() + timedelta(minutes=5),
    )
    session.add(transport_block)
    session.flush()
    with pytest.raises(ValueError, match="transport_blocked"):
        validate_clone_dispatch(
            session,
            action,
            account=session.get(TgAccount, identity.account_id),
            payload=GroupCloneSendPayload.model_validate(action.payload),
        )
    session.delete(transport_block)
    session.flush()

    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_raw_mtproto_message",
        lambda *args, **kwargs: SendResult(True, remote_message_id="7001", remote_mutation_started=True),
    )
    assert dispatch_action(session, action, project_task_stats=False) is True
    obligation = session.get(CloneDeliveryObligation, action.obligation_id)
    message_part = session.scalar(select(CloneMessagePart).where(CloneMessagePart.obligation_id == obligation.id))
    outbound_mapping = session.scalar(select(TelegramOutboundRandomIdMapping).where(
        TelegramOutboundRandomIdMapping.gateway_mutation_identity_id == identity.id,
    ))
    assert action.status == "success"
    assert obligation.state == "succeeded"
    assert message_part is not None and message_part.target_message_id == 7001
    assert outbound_mapping is not None and outbound_mapping.remote_message_or_topic_id == "7001"
    mappings = client.get(f"/api/tasks/{task_id}/clone-message-mappings", headers=headers)
    assert mappings.status_code == 200
    assert mappings.json()["items"][0]["remote_fact_id"]
    ingress_status = client.get(f"/api/tasks/{task_id}/clone-update-ingress-status", headers=headers)
    assert ingress_status.status_code == 200
    assert ingress_status.json()["owner_lease_healthy"] is True
    reconcile_cases = client.get(f"/api/tasks/{task_id}/clone-reconcile-cases", headers=headers)
    assert reconcile_cases.status_code == 200 and reconcile_cases.json()["items"] == []

    session.add(CloneSourceEvent(
        tenant_id=1,
        task_id=task_id,
        task_lifecycle_epoch=1,
        source_peer_type="channel",
        source_peer_id="-100111",
        source_message_id=502,
        event_type="message_new",
        event_identity_hash="event-502",
        apply_order_key="0002",
        stream_order_no=2,
        sender_peer_type="user",
        sender_peer_id="source-user-502",
        media_type="text",
        content="recover after timeout",
        entities=[],
        content_fingerprint="content-502",
        config_revision=2,
    ))
    session.flush()
    assert build_clone_plan(session, task) == 1
    action2 = session.scalar(select(Action).where(
        Action.task_id == task_id,
        Action.obligation_id != action.obligation_id,
    ))
    identity2 = session.scalar(select(TelegramGatewayMutationIdentity).where(
        TelegramGatewayMutationIdentity.obligation_id == action2.obligation_id,
    ))
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_raw_mtproto_message",
        lambda *args, **kwargs: SendResult(
            False,
            failure_type="rpc_timeout",
            detail="response lost",
            remote_mutation_started=None,
        ),
    )
    assert dispatch_action(session, action2, project_task_stats=False) is True
    assert action2.status == "unknown_after_send"

    state2 = session.scalar(select(TelegramAuthorizationUpdateState).where(
        TelegramAuthorizationUpdateState.authorization_id == identity2.authorization_id,
    ))
    state2.common_pts = 10
    state2.common_date = 1
    state2.owner_id = "expired-owner"
    state2.lease_expires_at = _now() - timedelta(seconds=1)
    session.commit()
    update_key = f"outbound:{identity2.random_id}:7002"
    recovered = TelegramDifferenceBatch(
        scope="common",
        status="live",
        cursor={"pts": 11, "qts": 0, "date": 2, "seq": 2},
        updates=(TelegramNormalizedUpdate(update_key, "UpdateMessageID"),),
        outbound_mappings=(TelegramOutboundMessageMapping(
            identity2.random_id,
            7002,
            update_key,
        ),),
    )
    monkeypatch.setattr(
        "app.services.task_center.telegram_update_collector.gateway.fetch_raw_authorization_difference",
        lambda *_args, **_kwargs: recovered,
    )
    reader_factory = sessionmaker(bind=session.get_bind())
    drain_result = drain_telegram_update_collector(reader_factory, tenant_id=1)
    assert drain_result.reconciled_count == 1
    with reader_factory() as reader:
        assert reader.get(Action, action2.id).status == "success"
        obligation2 = reader.get(CloneDeliveryObligation, action2.obligation_id)
        part2 = reader.scalar(select(CloneMessagePart).where(
            CloneMessagePart.obligation_id == action2.obligation_id,
        ))
        assert obligation2.state == "succeeded"
        assert part2 is not None and part2.target_message_id == 7002


def test_api_sequencer_head_decision(client_and_session):
    client, session = client_and_session
    headers = _auth_headers()

    task = Task(
        id="task-case-1",
        tenant_id=1,
        name="Case Task",
        type="group_clone",
        status="running",
    )
    session.add(task)
    session.flush()

    obl = CloneDeliveryObligation(
        id="obl-99",
        tenant_id=1,
        task_id=task.id,
        epoch=1,
        source_event_id="ev_dummy",
        obligation_kind="send",
        stream_order_no=1,
        sequencer_id=1,
        planned_at=datetime.now(timezone.utc),
        state="ready",
    )
    session.add(obl)
    session.flush()

    case = CloneSequencerHeadCase(
        id="case-100",
        task_id=task.id,
        epoch=1,
        sequencer_id=1,
        obligation_id=obl.id,
        case_kind="failed_terminal",
        policy_snapshot="fail_stop",
        state="waiting_decision",
    )
    session.add(case)
    session.commit()

    # 决策：accept_visible_gap
    decision_payload = {
        "expected_case_revision": 1,
        "decision": "accept_visible_gap",
        "reason": "人工审核放行并接受缺口",
        "client_request_id": "sequencer-case-request-1",
    }
    res = client.post(f"/api/tasks/{task.id}/clone-sequencer-head-cases/{case.id}/decision", json=decision_payload, headers=headers)
    assert res.status_code == 200
    assert res.json()["state"] == "visible_gap_accepted"
    replay = client.post(
        f"/api/tasks/{task.id}/clone-sequencer-head-cases/{case.id}/decision",
        json=decision_payload,
        headers=headers,
    )
    assert replay.status_code == 200
    assert replay.json() == res.json()


def test_api_cutover_preview(client_and_session):
    client, session = client_and_session
    headers = _auth_headers()

    # 存量 1->1 relay task
    legacy_task = Task(
        id="task-legacy-relay",
        tenant_id=1,
        name="Legacy Relay",
        type="group_relay",
        status="running",
        type_config={
            "source_groups": [{"group_id": -100111}],
            "target_group_ids": [-100222],
        },
        config_revision=1,
    )
    session.add(legacy_task)
    session.commit()

    res = client.post(f"/api/tasks/{legacy_task.id}/group-clone/cutover/preview", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "preview_token" in data
    assert data["legacy_task_id"] == legacy_task.id
    apply_payload = {
        "preview_token": data["preview_token"],
        "legacy_task_id": legacy_task.id,
        "expected_legacy_revision": data["expected_legacy_revision"],
        "route_manifest_hash": data["route_manifest_hash"],
        "expected_authority_version": data["expected_authority_version"],
        "open_action_fingerprint": data["open_action_fingerprint"],
        "client_request_id": "cutover-request-1",
        "reason": "迁移为 1 对 1 群克隆",
        "clone_config": _cutover_clone_payload(),
    }
    applied = client.post(
        f"/api/tasks/{legacy_task.id}/group-clone/cutover/apply",
        json=apply_payload,
        headers=headers,
    )
    assert applied.status_code == 200, applied.text
    clone_id = applied.json()["clone_task_id"]
    assert session.get(Task, legacy_task.id).status == "paused"
    assert session.get(Task, clone_id).status == "pending"

    rollback_preview = client.post(
        f"/api/tasks/{clone_id}/group-clone/rollback/preview", headers=headers,
    )
    assert rollback_preview.status_code == 200, rollback_preview.text
    rollback_data = rollback_preview.json()
    rollback = client.post(
        f"/api/tasks/{clone_id}/group-clone/rollback/apply",
        json={
            "preview_token": rollback_data["preview_token"],
            "clone_task_id": clone_id,
            "expected_authority_version": rollback_data["expected_authority_version"],
            "open_action_fingerprint": rollback_data["open_action_fingerprint"],
            "client_request_id": "rollback-request-1",
            "reason": "canary 前回滚",
        },
        headers=headers,
    )
    assert rollback.status_code == 200, rollback.text
    assert session.get(Task, legacy_task.id).status == "running"
    assert session.get(Task, clone_id).status == "paused"


def _cutover_clone_payload() -> dict:
    return {
        "name": "Cutover Clone",
        "client_request_id": "cutover-clone-create-1",
        "source": {
            "internal_group_id": 21,
            "operation_target_id": 11,
            "peer_type": "channel",
            "peer_id": "-100111",
            "listener_account_id": 101,
            "authorization_id": 201,
            "authorization_mode": "admin_authorized",
        },
        "target": {
            "internal_group_id": 22,
            "operation_target_id": 12,
            "peer_type": "channel",
            "peer_id": "-100222",
            "control_account_id": 102,
            "control_authorization_id": 202,
        },
        "sender_pool": {"account_ids": [101, 102, 103]},
        "pacing": {
            "min_delay_ms": 2000,
            "max_delay_ms": 5000,
            "strict_target_order": True,
        },
        "content": {"rule_set_id": 31, "rule_set_version": 1},
        "lifecycle": {
            "start_mode": "start_from_now",
            "failure_order_policy": "fail_stop",
        },
    }
