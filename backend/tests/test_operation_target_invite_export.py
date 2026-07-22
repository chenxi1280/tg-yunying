from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram.contracts import GroupSnapshot, InviteLinkResult
from app.models import AuditLog, OperationTarget, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.services.operations import canonicalize_operation_target_peer, export_operation_target_invite_link
from app.services.task_center.channel_membership import _joinable_channel_reference


pytestmark = pytest.mark.no_postgres


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
        def list_groups(self, *_args, **_kwargs):
            return [
                GroupSnapshot(
                    tg_peer_id="-1003573333444",
                    title="郑州楼凤",
                    group_type="supergroup",
                    member_count=1200,
                    permission_label="可发言",
                    can_send=True,
                    username="zhengzhou167",
                )
            ]

    monkeypatch.setattr("app.services.operations.gateway", FakeGateway())
    monkeypatch.setattr("app.services.operations.credentials_for_account", lambda *args, **kwargs: None)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        target = OperationTarget(id=2785, tenant_id=1, target_type="group", tg_peer_id="https://t.me/zhengzhou167", title="郑州楼凤")
        group = TgGroup(id=2806, tenant_id=1, tg_peer_id=target.tg_peer_id, title=target.title, can_send=True)
        observer = TgAccount(id=11, tenant_id=1, display_name="观察账号", phone_masked="11", status="在线", session_ciphertext="session")
        session.add_all([target, group, observer, TgGroupAccount(tenant_id=1, group_id=group.id, account_id=observer.id, can_send=True)])
        session.commit()

        result = canonicalize_operation_target_peer(session, 1, target.id, observer.id, "tester")
        session.refresh(target)
        session.refresh(group)

        assert result["stable_peer_id"] == "-1003573333444"
        assert target.tg_peer_id == "-1003573333444"
        assert target.username == "zhengzhou167"
        assert group.id == 2806
        assert group.tg_peer_id == target.tg_peer_id
        assert session.scalars(select(TgGroup)).all() == [group]
        assert session.scalar(select(AuditLog.action).where(AuditLog.target_id == str(target.id))) == "规范化运营目标 Telegram Peer"


def test_canonicalize_legacy_public_target_peer_rejects_existing_peer(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    class FakeGateway:
        def list_groups(self, *_args, **_kwargs):
            return [GroupSnapshot("-1003573333444", "郑州楼凤", "supergroup", 1200, "可发言", True, username="zhengzhou167")]

    monkeypatch.setattr("app.services.operations.gateway", FakeGateway())
    monkeypatch.setattr("app.services.operations.credentials_for_account", lambda *args, **kwargs: None)

    with Session(engine) as session:
        session.add_all([
            Tenant(id=1, name="默认运营空间"),
            OperationTarget(id=2785, tenant_id=1, target_type="group", tg_peer_id="https://t.me/zhengzhou167", title="郑州楼凤"),
            TgGroup(id=2806, tenant_id=1, tg_peer_id="https://t.me/zhengzhou167", title="郑州楼凤", can_send=True),
            TgAccount(id=11, tenant_id=1, display_name="观察账号", phone_masked="11", status="在线", session_ciphertext="session"),
            TgGroupAccount(tenant_id=1, group_id=2806, account_id=11, can_send=True),
            OperationTarget(id=999, tenant_id=1, target_type="group", tg_peer_id="-1003573333444", title="已存在目标"),
        ])
        session.commit()

        with pytest.raises(ValueError, match="stable Telegram peer already assigned"):
            canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")

        assert session.get(OperationTarget, 2785).tg_peer_id == "https://t.me/zhengzhou167"
