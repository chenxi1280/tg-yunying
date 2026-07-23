from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram.contracts import GroupSnapshot, InviteLinkResult
from app.models import (
    Action,
    AiAccountGroupStanceMemory,
    AiGroupMessageMemory,
    AuditLog,
    Campaign,
    ManualOperationRecord,
    MessageFingerprint,
    OperationTarget,
    SearchJoinLinkedTaskDispatch,
    Task,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.services.operations import canonicalize_operation_target_peer, export_operation_target_invite_link
from app.services.task_center.channel_membership import _joinable_channel_reference


pytestmark = pytest.mark.no_postgres


def _patch_canonicalization_gateway(monkeypatch) -> None:
    class FakeGateway:
        def resolve_group_by_public_username(self, *_args, **_kwargs):
            return GroupSnapshot("-1003573333444", "郑州楼凤", "supergroup", 1200, "可发言", True, username="zhengzhou167")

    monkeypatch.setattr("app.services.operations.gateway", FakeGateway())
    monkeypatch.setattr("app.services.operations.credentials_for_account", lambda *args, **_kwargs: None)


def _seed_duplicate_peer_pair(session: Session) -> None:
    session.add_all([
        Tenant(id=1, name="默认运营空间"),
        OperationTarget(id=2785, tenant_id=1, target_type="group", tg_peer_id="https://t.me/zhengzhou167", title="郑州楼凤"),
        TgGroup(id=2806, tenant_id=1, tg_peer_id="https://t.me/zhengzhou167", title="郑州楼凤", can_send=True, daily_limit=675, listener_enabled=True),
        OperationTarget(id=2790, tenant_id=1, target_type="group", tg_peer_id="-1003573333444", title="重复目标", username="zhengzhou167"),
        TgGroup(id=2810, tenant_id=1, tg_peer_id="-1003573333444", title="重复群", can_send=True),
        TgAccount(id=11, tenant_id=1, display_name="观察账号", phone_masked="11", status="在线", session_ciphertext="session"),
        TgAccount(id=12, tenant_id=1, display_name="补充账号", phone_masked="12", status="在线", session_ciphertext="session"),
        TgGroupAccount(tenant_id=1, group_id=2806, account_id=11, can_send=True),
        TgGroupAccount(tenant_id=1, group_id=2810, account_id=12, can_send=True),
    ])


def test_export_operation_target_invite_link_updates_join_ref(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    class FakeGateway:
        def export_group_invite_link(self, *args, **kwargs):
            return InviteLinkResult(True, detail="https://t.me/+validInvite", invite_link="https://t.me/+validInvite")

    monkeypatch.setattr("app.services.operations.gateway", FakeGateway())
    monkeypatch.setattr("app.services.operations.credentials_for_account", lambda *args, **kwargs: None)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(
            id=485,
            tenant_id=1,
            target_type="group",
            tg_peer_id="-1003583171851",
            title="天津音乐学院",
            username="zzjinli",
            can_send=True,
            auth_status="已授权运营",
        )
        group = TgGroup(id=484, tenant_id=1, tg_peer_id=target.tg_peer_id, title=target.title, can_send=True, auth_status="已授权运营")
        account = TgAccount(id=10, tenant_id=1, display_name="账号10", phone_masked="10", status="在线", session_ciphertext="session")
        link = TgGroupAccount(tenant_id=1, group_id=group.id, account_id=account.id, can_send=True)
        session.add_all([target, group, account, link])
        session.commit()

        result = export_operation_target_invite_link(session, 1, target.id, "tester")
        session.refresh(target)

    assert result["invite_link"] == "https://t.me/+validInvite"
    assert target.username == "https://t.me/+validInvite"
    assert _joinable_channel_reference(target) == "https://t.me/+validInvite"


def test_export_operation_target_invite_link_prefers_configured_rescue_admin(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    exported_by: list[int] = []

    class FakeGateway:
        def export_group_invite_link(self, account_id, *args, **kwargs):
            exported_by.append(account_id)
            return InviteLinkResult(True, detail="https://t.me/+rescueInvite", invite_link="https://t.me/+rescueInvite")

    monkeypatch.setattr("app.services.operations.gateway", FakeGateway())
    monkeypatch.setattr("app.services.operations.credentials_for_account", lambda *args, **kwargs: None)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间", group_rescue_enabled=True, group_rescue_admin_account_id=515))
        target = OperationTarget(id=485, tenant_id=1, target_type="group", tg_peer_id="-1003583171851", title="天津音乐学院")
        group = TgGroup(id=484, tenant_id=1, tg_peer_id=target.tg_peer_id, title=target.title, can_send=True)
        primary = TgAccount(id=10, tenant_id=1, display_name="账号10", phone_masked="10", status="在线", session_ciphertext="session")
        rescue = TgAccount(id=515, tenant_id=1, display_name="救援账号", phone_masked="515", status="在线", session_ciphertext="session")
        session.add_all([
            target,
            group,
            primary,
            rescue,
            TgGroupAccount(tenant_id=1, group_id=group.id, account_id=primary.id, can_send=True),
            TgGroupAccount(tenant_id=1, group_id=group.id, account_id=rescue.id, can_send=True),
        ])
        session.commit()

        export_operation_target_invite_link(session, 1, target.id, "tester")

    assert exported_by == [515]


def test_canonicalize_legacy_public_target_peer_keeps_existing_group(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    class FakeGateway:
        def resolve_group_by_public_username(self, *_args, **_kwargs):
            return GroupSnapshot(
                tg_peer_id="-1003573333444",
                title="郑州楼凤",
                group_type="supergroup",
                member_count=1200,
                permission_label="可发言",
                can_send=True,
                username="zhengzhou167",
            )

    monkeypatch.setattr("app.services.operations.gateway", FakeGateway())
    monkeypatch.setattr("app.services.operations.credentials_for_account", lambda *args, **kwargs: None)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(id=2785, tenant_id=1, target_type="group", tg_peer_id="https://t.me/zhengzhou167", title="郑州楼凤")
        group = TgGroup(id=2806, tenant_id=1, tg_peer_id=target.tg_peer_id, title=target.title, can_send=True)
        observer = TgAccount(id=11, tenant_id=1, display_name="观察账号", phone_masked="11", status="在线", session_ciphertext="session")
        session.add_all([target, group, observer, TgGroupAccount(tenant_id=1, group_id=group.id, account_id=observer.id, can_send=True)])
        session.commit()

        result = canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")
        session.refresh(target)
        session.refresh(group)

        assert result["stable_peer_id"] == "-1003573333444"
        assert target.tg_peer_id == "-1003573333444"
        assert target.username == "zhengzhou167"
        assert group.id == 2806
        assert group.tg_peer_id == target.tg_peer_id
        assert session.scalars(select(TgGroup)).all() == [group]
        assert session.scalar(select(AuditLog.action).where(AuditLog.target_id == str(target.id))) == "规范化运营目标 Telegram Peer"


def test_canonicalize_legacy_public_target_peer_merges_unreferenced_duplicate(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    class FakeGateway:
        def resolve_group_by_public_username(self, *_args, **_kwargs):
            return GroupSnapshot("-1003573333444", "郑州楼凤", "supergroup", 1200, "可发言", True, username="zhengzhou167")

    monkeypatch.setattr("app.services.operations.gateway", FakeGateway())
    monkeypatch.setattr("app.services.operations.credentials_for_account", lambda *args, **_kwargs: None)

    with Session(engine) as session:
        session.add_all([
            Tenant(id=1, name="默认运营空间"),
            OperationTarget(id=2785, tenant_id=1, target_type="group", tg_peer_id="https://t.me/zhengzhou167", title="郑州楼凤"),
            TgGroup(id=2806, tenant_id=1, tg_peer_id="https://t.me/zhengzhou167", title="郑州楼凤", can_send=True, daily_limit=675, listener_enabled=True),
            OperationTarget(id=2790, tenant_id=1, target_type="group", tg_peer_id="-1003573333444", title="重复目标", username="zhengzhou167"),
            TgGroup(id=2810, tenant_id=1, tg_peer_id="-1003573333444", title="重复群", can_send=True),
            TgAccount(id=11, tenant_id=1, display_name="观察账号", phone_masked="11", status="在线", session_ciphertext="session"),
            TgAccount(id=12, tenant_id=1, display_name="补充账号", phone_masked="12", status="在线", session_ciphertext="session"),
            TgGroupAccount(tenant_id=1, group_id=2806, account_id=11, can_send=True),
            TgGroupAccount(tenant_id=1, group_id=2810, account_id=11, can_send=True, permission_label="可发言"),
            TgGroupAccount(tenant_id=1, group_id=2810, account_id=12, can_send=True),
        ])
        session.commit()

        result = canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")
        target = session.get(OperationTarget, 2785)
        group = session.get(TgGroup, 2806)
        links = session.scalars(select(TgGroupAccount).where(TgGroupAccount.group_id == 2806)).all()

        assert result["merged_duplicate_target_id"] == 2790
        assert result["merged_duplicate_group_id"] == 2810
        assert target.tg_peer_id == "-1003573333444"
        assert group.daily_limit == 675
        assert group.listener_enabled is True
        assert session.get(OperationTarget, 2790) is None
        assert session.get(TgGroup, 2810) is None
        assert sorted(link.account_id for link in links) == [11, 12]


def test_canonicalize_legacy_public_target_peer_rejects_existing_peer(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    class FakeGateway:
        def resolve_group_by_public_username(self, *_args, **_kwargs):
            return GroupSnapshot("-1003573333444", "郑州楼凤", "supergroup", 1200, "可发言", True, username="zhengzhou167")

    monkeypatch.setattr("app.services.operations.gateway", FakeGateway())
    monkeypatch.setattr("app.services.operations.credentials_for_account", lambda *args, **kwargs: None)

    with Session(engine) as session:
        session.add_all([
            Tenant(id=1, name="默认运营空间"),
            OperationTarget(id=2785, tenant_id=1, target_type="group", tg_peer_id="https://t.me/zhengzhou167", title="郑州楼凤"),
            TgGroup(id=2806, tenant_id=1, tg_peer_id="https://t.me/zhengzhou167", title="郑州楼凤", can_send=True),
            TgAccount(id=11, tenant_id=1, display_name="观察账号", phone_masked="11", status="在线", session_ciphertext="session"),
            TgGroupAccount(tenant_id=1, group_id=2806, account_id=11, can_send=True),
            OperationTarget(id=999, tenant_id=1, target_type="group", tg_peer_id="-1003573333444", title="已存在目标", username="zhengzhou167"),
            TgGroup(id=1000, tenant_id=1, tg_peer_id="-1003573333444", title="已存在群", can_send=True),
            ManualOperationRecord(tenant_id=1, account_id=11, target_id=999, operation_type="MESSAGE_SEND", status="success"),
        ])
        session.commit()

        with pytest.raises(ValueError, match="duplicate target has business references"):
            canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")

        assert session.get(OperationTarget, 2785).tg_peer_id == "https://t.me/zhengzhou167"


def test_canonicalize_legacy_peer_rejects_nested_task_group_reference(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _patch_canonicalization_gateway(monkeypatch)

    with Session(engine) as session:
        _seed_duplicate_peer_pair(session)
        session.add(Task(id="task-1", tenant_id=1, name="引用重复群", type="group_relay", type_config={"source_groups": [{"group_id": 2810}]}))
        session.commit()

        with pytest.raises(ValueError, match="duplicate pair is used by a task"):
            canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")

        assert session.get(TgGroup, 2810) is not None


def test_canonicalize_legacy_peer_rejects_csv_task_group_reference(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _patch_canonicalization_gateway(monkeypatch)

    with Session(engine) as session:
        _seed_duplicate_peer_pair(session)
        session.add(Task(
            id="task-csv",
            tenant_id=1,
            name="引用重复群",
            type="group_relay",
            type_config={"target_group_ids": "2810,9999"},
        ))
        session.commit()

        with pytest.raises(ValueError, match="duplicate pair is used by a task"):
            canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")


def test_canonicalize_legacy_peer_rejects_non_foreign_group_state(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _patch_canonicalization_gateway(monkeypatch)

    with Session(engine) as session:
        _seed_duplicate_peer_pair(session)
        memory = AiGroupMessageMemory(id="memory-1", tenant_id=1, group_id=2810)
        session.add(memory)
        session.commit()

        with pytest.raises(ValueError, match="duplicate group has business references"):
            canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")

        session.delete(memory)
        session.add(AiAccountGroupStanceMemory(id="stance-1", tenant_id=1, group_id=2810, account_id=11))
        session.commit()
        with pytest.raises(ValueError, match="duplicate group has business references"):
            canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")


@pytest.mark.parametrize(
    "source_group_id",
    [
        "2810",
        "task-relay:relay:2810:target:9",
        "task-relay:relay:7:target:2810",
        "task-ai:group_ai_chat:2810",
    ],
)
def test_canonicalize_legacy_peer_rejects_fingerprint_group_reference(monkeypatch, source_group_id: str) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _patch_canonicalization_gateway(monkeypatch)

    with Session(engine) as session:
        _seed_duplicate_peer_pair(session)
        session.add(MessageFingerprint(
            id="fingerprint-1",
            tenant_id=1,
            source_group_id=source_group_id,
            fingerprint="fingerprint",
        ))
        session.commit()

        with pytest.raises(ValueError, match="duplicate group has business references"):
            canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")


def test_canonicalize_legacy_peer_rejects_cross_tenant_group_link(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _patch_canonicalization_gateway(monkeypatch)

    with Session(engine) as session:
        _seed_duplicate_peer_pair(session)
        session.add_all([
            Tenant(id=2, name="第二租户"),
            TgAccount(id=21, tenant_id=2, display_name="跨租户账号", phone_masked="21", status="在线", session_ciphertext="session"),
            TgGroupAccount(tenant_id=2, group_id=2810, account_id=21, can_send=True),
        ])
        session.commit()

        with pytest.raises(ValueError, match="cross-tenant account link"):
            canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")

        assert session.get(TgGroup, 2810) is not None


def test_canonicalize_legacy_peer_keeps_historical_duplicate_audit(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _patch_canonicalization_gateway(monkeypatch)

    with Session(engine) as session:
        _seed_duplicate_peer_pair(session)
        session.add(AuditLog(tenant_id=1, actor="tester", action="历史记录", target_type="operation_target", target_id="2790"))
        session.commit()

        result = canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")

        assert result["merged_duplicate_target_id"] == 2790
        assert session.get(OperationTarget, 2790) is None
        assert session.scalar(select(AuditLog.id).where(AuditLog.target_id == "2790")) is not None


def test_canonicalize_legacy_peer_rolls_back_concurrent_peer_conflict(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _patch_canonicalization_gateway(monkeypatch)

    with Session(engine) as session:
        _seed_duplicate_peer_pair(session)
        session.commit()

        def raise_conflict() -> None:
            raise IntegrityError("UPDATE operation_targets", {}, RuntimeError("duplicate key"))

        monkeypatch.setattr(session, "commit", raise_conflict)
        with pytest.raises(ValueError, match="peer canonicalization transaction rolled back"):
            canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")

        assert session.get(OperationTarget, 2785).tg_peer_id == "https://t.me/zhengzhou167"
        assert session.get(OperationTarget, 2790) is not None


def test_canonicalize_legacy_peer_requires_fresh_session(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _patch_canonicalization_gateway(monkeypatch)

    with Session(engine) as session:
        _seed_duplicate_peer_pair(session)
        session.commit()
        session.scalar(select(OperationTarget.id).where(OperationTarget.id == 2785))

        with pytest.raises(ValueError, match="requires a fresh database session"):
            canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")


@pytest.mark.parametrize(
    ("status", "blocked"),
    (
        ("pending", True),
        ("claiming", True),
        ("executing", True),
        ("retryable_failed", True),
        ("unknown_after_send", True),
        ("waiting_cache", True),
        ("failed", True),
        ("success", False),
        ("skipped", False),
    ),
)
def test_canonicalize_legacy_peer_blocks_active_and_failed_action_payload_reference(monkeypatch, status: str, blocked: bool) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _patch_canonicalization_gateway(monkeypatch)

    with Session(engine) as session:
        _seed_duplicate_peer_pair(session)
        session.add_all([
            Task(id="task-action", tenant_id=1, name="引用重复群", type="group_ai_chat"),
            Action(
                id="action-1",
                tenant_id=1,
                task_id="task-action",
                task_type="group_ai_chat",
                action_type="send_message",
                status=status,
                payload={"routing": {"target_group_ids": [2810]}},
            ),
        ])
        session.commit()

        if blocked:
            with pytest.raises(ValueError, match="duplicate pair is used by a task"):
                canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")
        else:
            result = canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")

    if not blocked:
        assert result["merged_duplicate_target_id"] == 2790
        assert result["merged_duplicate_group_id"] == 2810


def test_canonicalize_legacy_peer_rejects_campaign_csv_reference(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _patch_canonicalization_gateway(monkeypatch)

    with Session(engine) as session:
        _seed_duplicate_peer_pair(session)
        session.add(Campaign(
            id=1,
            tenant_id=1,
            group_id=2806,
            title="引用重复群",
            campaign_type="mirror_forward",
            topic="测试",
            source_group_ids="2810",
        ))
        session.commit()

        with pytest.raises(ValueError, match="duplicate group has business references"):
            canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")


def test_canonicalize_legacy_peer_rejects_campaign_selected_account_reference(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _patch_canonicalization_gateway(monkeypatch)

    with Session(engine) as session:
        _seed_duplicate_peer_pair(session)
        session.add(Campaign(
            id=1,
            tenant_id=1,
            group_id=2806,
            title="引用重复群",
            campaign_type="ai_activity",
            topic="测试",
            selected_account_ids_by_group='{"2810": [12]}',
        ))
        session.commit()

        with pytest.raises(ValueError, match="duplicate group has business references"):
            canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")


def test_canonicalize_legacy_peer_rejects_search_join_group_reference(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _patch_canonicalization_gateway(monkeypatch)

    with Session(engine) as session:
        _seed_duplicate_peer_pair(session)
        session.add(SearchJoinLinkedTaskDispatch(
            id="dispatch-1",
            tenant_id=1,
            target_group_id=2810,
        ))
        session.commit()

        with pytest.raises(ValueError, match="duplicate group has business references"):
            canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")
