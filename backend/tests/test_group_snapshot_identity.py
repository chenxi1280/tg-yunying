from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram.contracts import GroupSnapshot
from app.models import AccountStatus, OperationTarget, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.services import accounts, operations


pytestmark = pytest.mark.no_postgres


def test_group_sync_reuses_legacy_public_target_for_numeric_snapshot(monkeypatch) -> None:
    snapshot = GroupSnapshot(
        tg_peer_id="-1002300",
        title="路由群",
        group_type="supergroup",
        member_count=100,
        permission_label="可发言",
        can_send=True,
        username="route_alias",
    )
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(accounts, "credentials_for_account", lambda *_args: None)
    monkeypatch.setattr(operations, "credentials_for_account", lambda *_args: None)
    monkeypatch.setattr(accounts.gateway, "list_groups", lambda *_args: [snapshot])
    monkeypatch.setattr(operations.gateway, "list_groups", lambda *_args: [snapshot])

    with Session(engine) as session:
        legacy_peer = "https://t.me/route_alias"
        session.add_all([
            Tenant(id=1, name="租户"),
            TgAccount(
                id=10,
                tenant_id=1,
                display_name="观察账号",
                phone_masked="10",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="session",
            ),
            TgGroup(id=21, tenant_id=1, tg_peer_id=legacy_peer, title="路由群"),
            OperationTarget(
                id=31,
                tenant_id=1,
                target_type="group",
                tg_peer_id=legacy_peer,
                title="路由群",
            ),
        ])
        session.commit()

        groups = accounts.sync_groups(session, 10, actor="test")
        operations.sync_account_targets(session, 10, actor="test")

        assert [group.id for group in groups] == [21]
        assert session.scalars(select(TgGroup).order_by(TgGroup.id)).all()[0].tg_peer_id == legacy_peer
        assert session.scalars(select(OperationTarget).order_by(OperationTarget.id)).all()[0].tg_peer_id == legacy_peer
        assert session.scalar(select(TgGroupAccount.group_id)) == 21


def test_group_sync_reuses_legacy_public_group_without_target(monkeypatch) -> None:
    snapshot = GroupSnapshot(
        tg_peer_id="-1002301",
        title="只有群资源",
        group_type="supergroup",
        member_count=100,
        permission_label="可发言",
        can_send=True,
        username="group_only_alias",
    )
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(accounts, "credentials_for_account", lambda *_args: None)
    monkeypatch.setattr(accounts.gateway, "list_groups", lambda *_args: [snapshot])
    monkeypatch.setattr(operations, "credentials_for_account", lambda *_args: None)
    monkeypatch.setattr(operations.gateway, "list_groups", lambda *_args: [snapshot])

    with Session(engine) as session:
        session.add_all([
            Tenant(id=1, name="租户"),
            TgAccount(
                id=10,
                tenant_id=1,
                display_name="观察账号",
                phone_masked="10",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="session",
            ),
            TgGroup(
                id=21,
                tenant_id=1,
                tg_peer_id="@group_only_alias",
                title="只有群资源",
            ),
        ])
        session.commit()

        groups = accounts.sync_groups(session, 10, actor="test")
        operations.sync_account_targets(session, 10, actor="test")

        assert [group.id for group in groups] == [21]
        assert session.scalars(select(TgGroup).order_by(TgGroup.id)).all()[0].tg_peer_id == "@group_only_alias"
        assert session.scalars(select(OperationTarget).order_by(OperationTarget.id)).all()[0].tg_peer_id == "@group_only_alias"
