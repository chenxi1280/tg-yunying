"""Membership effects belong to the original operation target, never a title alias."""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, GroupBotAdmission, OperationTarget, Task, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.services.task_center import dispatcher
from app.services.task_center.payloads import EnsureChannelMembershipPayload

pytestmark = pytest.mark.no_postgres


@pytest.fixture
def membership_scope():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="test"))
        target = OperationTarget(id=21, tenant_id=1, target_type="group",
            tg_peer_id="https://t.me/public_alias", title="same title", can_send=False)
        canonical = TgGroup(id=31, tenant_id=1, tg_peer_id="https://t.me/public_alias",
            title="same title", can_send=False)
        alias = TgGroup(id=32, tenant_id=1, tg_peer_id="public_alias",
            title="same title", can_send=True)
        account = TgAccount(id=11, tenant_id=1, display_name="test", phone_masked="test")
        task = Task(id="canonical-join", tenant_id=1, name="test", type="group_ai_chat",
            type_config={"target_group_id":31,"target_operation_target_id":21})
        action = Action(id="joined-action", tenant_id=1, task_id=task.id,
            task_type="group_ai_chat", action_type="ensure_target_membership",
            account_id=11, result={"join_start_cursor":"9"})
        session.add_all([target, canonical, alias, account, task, action])
        session.flush()
        yield session, SimpleNamespace(target=target, canonical=canonical, alias=alias,
            account=account, task=task, action=action)


def _payload(reference):
    return EnsureChannelMembershipPayload(channel_id=reference, channel_target_id=21,
        target_type="group", target_display="same title", require_send=True)


@pytest.mark.parametrize("reference", ["public_alias", "https://t.me/public_alias", "@public_alias"])
def test_join_success_projects_canonical_link_and_independent_bot_observation(
    membership_scope, reference,
):
    session, scope = membership_scope

    dispatcher._mark_membership_joined(session, scope.action, scope.account, _payload(reference))
    session.flush()

    links = list(session.scalars(select(TgGroupAccount)))
    assert [(link.group_id, link.account_id, link.can_send) for link in links] == [(31, 11, True)]
    admission = session.scalar(select(GroupBotAdmission))
    assert admission.group_id == 31
    assert admission.state == "awaiting_group_bot_rule"
    assert admission.membership_action_id == scope.action.id
    assert scope.action.result["group_bot_admission_id"] == admission.id
    assert scope.canonical.can_send is True
    assert scope.alias.can_send is True


def test_permission_denial_does_not_overwrite_same_title_group(membership_scope):
    session, scope = membership_scope

    dispatcher._record_group_send_permission_denied(
        session, scope.action, scope.account, _payload("public_alias"), "denied",
    )
    session.flush()

    links = list(session.scalars(select(TgGroupAccount)))
    assert [(link.group_id, link.can_send) for link in links] == [(31, False)]
    assert scope.alias.can_send is True
    assert session.scalar(select(GroupBotAdmission)) is None


def test_canonical_group_absence_creates_original_identity_without_reusing_alias(membership_scope):
    session, scope = membership_scope
    session.delete(scope.canonical)
    session.flush()

    group = dispatcher._membership_group_for_payload(
        session, scope.target, _payload("public_alias"), create=True,
    )

    assert group.tg_peer_id == scope.target.tg_peer_id
    assert group.id != scope.alias.id
    assert session.scalar(select(TgGroupAccount)) is None


def test_different_actual_peer_is_not_projected_as_task_target(membership_scope):
    session, scope = membership_scope
    actual = TgGroup(id=33, tenant_id=1, tg_peer_id="-100999", title="actual", can_send=True)
    session.add(actual)
    session.flush()

    group = dispatcher._membership_group_for_payload(
        session, scope.target, _payload("-100999"), create=True,
    )

    assert group.id == actual.id
    assert session.scalar(select(TgGroupAccount)) is None
