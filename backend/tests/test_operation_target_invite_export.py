from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram.contracts import InviteLinkResult
from app.models import OperationTarget, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.services.operations import export_operation_target_invite_link
from app.services.task_center.channel_membership import _joinable_channel_reference


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
