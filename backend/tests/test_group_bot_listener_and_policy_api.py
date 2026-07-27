from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    GroupBotAdmission,
    GroupBotRequiredChannelFollow,
    GroupContextMessage,
    OperationTarget,
    Task,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services.group_listener_context_writer import insert_context_snapshots
from app.services.task_center.group_bot_admission import create_policy, ensure_admission_after_join, READY_STATE
from app.services.task_center.payloads import GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE

pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_listener_control_event_ingests_trusted_bot_before_context():
    with _session() as session:
        session.add(Tenant(id=1, name="t"))
        group = TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="g", group_type="supergroup")
        account = TgAccount(
            id=11,
            tenant_id=1,
            display_name="clementine",
            username="clementine",
            phone_masked="+100",
        )
        session.add_all([group, account])
        session.flush()
        ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=7,
            account_id=11,
            membership_action_id="join-1",
            join_start_cursor="10",
        )
        snapshot = SimpleNamespace(
            content="@clementine 请先关注 https://t.me/school_news",
            remote_message_id="99",
            sender_peer_id="900",
            sender_name="gatebot",
            sender_username="gatebot",
            is_bot=True,
            sender_role="admin",
            message_type="text",
            sent_at=None,
        )
        inserted = insert_context_snapshots(
            session,
            group,
            account,
            [snapshot],
            ignored_sender=lambda _s: False,
            create_source_media=False,
            learning_scene=None,
        )
        # Control bot rules are persisted only as bot audit context; AI readers filter is_bot.
        assert inserted == 1
        context = session.get(GroupContextMessage, 1)
        assert context is not None
        assert context.is_bot is True
        admission = session.scalar(
            select_admission(session, group_id=7, account_id=11)
        )
        assert admission is not None
        assert admission.state == "required_channel_follow_pending"
        assert "school_news" in (admission.required_channel_refs or [])


def test_unknown_bot_global_prompt_does_not_create_follow_actions_without_policy():
    with _session() as session:
        group, accounts = _seed_global_rule_scope(session)
        _seed_waiting_admissions(session, group.id, accounts)

        inserted = insert_context_snapshots(
            session,
            group,
            accounts[0],
            [_global_rule_snapshot()],
            ignored_sender=lambda _snapshot: False,
            create_source_media=False,
            learning_scene=None,
        )

        assert inserted == 1
        assert session.query(Action).filter(Action.action_type == GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE).count() == 0
        assert session.get(GroupBotAdmission, 1).state == "group_bot_rule_unattributed"


def test_audited_unknown_bot_global_prompt_creates_exact_follow_for_each_scoped_admission():
    with _session() as session:
        group, accounts = _seed_global_rule_scope(session)
        _seed_waiting_admissions(session, group.id, accounts)
        create_policy(
            session,
            tenant_id=1,
            group_id=group.id,
            completion_policy="explicit_bot_confirmation",
            trusted_bot_peer_id="900",
            reason="reviewed group bot control",
            evidence_ref="group-context:99",
            created_by="operator",
        )

        insert_context_snapshots(
            session,
            group,
            accounts[0],
            [_global_rule_snapshot()],
            ignored_sender=lambda _snapshot: False,
            create_source_media=False,
            learning_scene=None,
        )
        follows = list(
            session.query(Action)
            .filter(Action.action_type == GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE)
            .order_by(Action.account_id)
        )
        assert [action.account_id for action in follows] == [11, 12]
        assert all(action.payload["source_message_id"] == "99" for action in follows)
        assert all(action.payload["admission_bound_task_id"] == "task-ai" for action in follows)
        assert session.get(GroupBotAdmission, 1).state == "required_channel_follow_pending"
        assert session.get(GroupBotAdmission, 2).state == "required_channel_follow_pending"
        assert session.query(Action).filter(Action.action_type == "send_message").count() == 0


def test_audited_bot_prompt_with_unmatched_recipient_does_not_fan_out():
    with _session() as session:
        group, accounts = _seed_global_rule_scope(session)
        _seed_waiting_admissions(session, group.id, accounts)
        create_policy(
            session,
            tenant_id=1,
            group_id=group.id,
            completion_policy="explicit_bot_confirmation",
            trusted_bot_peer_id="900",
            reason="reviewed group bot control",
            evidence_ref="group-context:100",
            created_by="operator",
        )
        snapshot = _global_rule_snapshot()
        snapshot.content = "unrelated，您需要关注我们的频道才能发言。"
        snapshot.remote_message_id = "100"

        insert_context_snapshots(
            session,
            group,
            accounts[0],
            [snapshot],
            ignored_sender=lambda _snapshot: False,
            create_source_media=False,
            learning_scene=None,
        )

        assert session.query(Action).filter(Action.action_type == GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE).count() == 0
        assert session.get(GroupBotAdmission, 1).state == "group_bot_rule_unattributed"


def test_audited_repeated_unmatched_recipient_prompt_fans_out_standard_rule():
    with _session() as session:
        group, accounts = _seed_global_rule_scope(session)
        _seed_waiting_admissions(session, group.id, accounts)
        create_policy(
            session,
            tenant_id=1,
            group_id=group.id,
            completion_policy="explicit_bot_confirmation",
            trusted_bot_peer_id="900",
            reason="reviewed repeated group bot control",
            evidence_ref="group-context:100",
            created_by="operator",
        )
        first = _global_rule_snapshot()
        first.content = "unrelated-one，您需要关注我们的频道才能发言。"
        first.remote_message_id = "100"
        insert_context_snapshots(
            session,
            group,
            accounts[0],
            [first],
            ignored_sender=lambda _snapshot: False,
            create_source_media=False,
            learning_scene=None,
        )
        for admission in session.query(GroupBotAdmission).all():
            admission.source_message_id = "historical-wrong-source"
            admission.evidence_ref = "attr:trusted_repeatable_recipient_rule;msg:historical-wrong-source"
            session.add(
                Action(
                    id=f"historical-confirm-{admission.account_id}",
                    tenant_id=1,
                    task_id="task-ai",
                    task_type="group_ai_chat",
                    action_type="group_bot_confirmation_button",
                    account_id=admission.account_id,
                    status="pending",
                    payload={
                        "admission_id": admission.id,
                        "admission_version": 1,
                        "source_message_id": "historical-wrong-source",
                    },
                )
            )
        session.flush()

        second = _global_rule_snapshot()
        second.content = "unrelated-two，您需要关注我们的频道才能发言。"
        second.remote_message_id = "101"
        insert_context_snapshots(
            session,
            group,
            accounts[0],
            [second],
            ignored_sender=lambda _snapshot: False,
            create_source_media=False,
            learning_scene=None,
        )

        follows = list(
            session.query(Action)
            .filter(Action.action_type == GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE)
            .order_by(Action.account_id)
        )
        assert [action.account_id for action in follows] == [11, 12]
        assert all(action.payload["source_message_id"] == "101" for action in follows)
        confirmations = list(session.query(Action).filter(Action.action_type == "group_bot_confirmation_button"))
        assert all(action.status == "skipped" for action in confirmations)
        assert all(action.result["error_code"] == "group_bot_confirmation_superseded" for action in confirmations)
        assert session.get(GroupBotAdmission, 1).source_message_id == ""
        assert session.get(GroupBotAdmission, 2).source_message_id == ""


def test_unmatched_recipient_prompt_with_different_confirmation_shape_does_not_fan_out():
    with _session() as session:
        group, accounts = _seed_global_rule_scope(session)
        _seed_waiting_admissions(session, group.id, accounts)
        create_policy(
            session,
            tenant_id=1,
            group_id=group.id,
            completion_policy="explicit_bot_confirmation",
            trusted_bot_peer_id="900",
            reason="reviewed repeated group bot control",
            evidence_ref="group-context:100",
            created_by="operator",
        )
        first = _global_rule_snapshot()
        first.content = "unrelated-one，您需要关注我们的频道才能发言。"
        first.remote_message_id = "100"
        insert_context_snapshots(
            session,
            group,
            accounts[0],
            [first],
            ignored_sender=lambda _snapshot: False,
            create_source_media=False,
            learning_scene=None,
        )

        second = _global_rule_snapshot()
        second.content = "unrelated-two，您需要关注我们的频道才能发言。"
        second.remote_message_id = "101"
        second.control_buttons[1]["text"] = "我已关注"
        insert_context_snapshots(
            session,
            group,
            accounts[0],
            [second],
            ignored_sender=lambda _snapshot: False,
            create_source_media=False,
            learning_scene=None,
        )

        assert session.query(Action).filter(Action.action_type == GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE).count() == 0
        assert session.get(GroupBotAdmission, 1).state == "group_bot_rule_unattributed"


def test_repeated_standard_rule_preserves_only_account_owned_confirmation():
    with _session() as session:
        group, accounts = _seed_global_rule_scope(session)
        _seed_waiting_admissions(session, group.id, accounts)
        create_policy(
            session,
            tenant_id=1,
            group_id=group.id,
            completion_policy="explicit_bot_confirmation",
            trusted_bot_peer_id="900",
            reason="reviewed repeated group bot control",
            evidence_ref="group-context:100",
            created_by="operator",
        )
        owned = _global_rule_snapshot()
        owned.content = "first，您需要关注我们的频道才能发言。"
        owned.remote_message_id = "90"
        insert_context_snapshots(
            session,
            group,
            accounts[0],
            [owned],
            ignored_sender=lambda _snapshot: False,
            create_source_media=False,
            learning_scene=None,
        )
        for message_id, recipient in (("100", "unrelated-one"), ("101", "unrelated-two")):
            snapshot = _global_rule_snapshot()
            snapshot.content = f"{recipient}，您需要关注我们的频道才能发言。"
            snapshot.remote_message_id = message_id
            insert_context_snapshots(
                session,
                group,
                accounts[0],
                [snapshot],
                ignored_sender=lambda _snapshot: False,
                create_source_media=False,
                learning_scene=None,
            )

        confirmations = list(session.query(Action).filter(Action.action_type == "group_bot_confirmation_button"))
        assert [(action.account_id, action.payload["source_message_id"]) for action in confirmations] == [(11, "90")]
        assert session.get(GroupBotAdmission, 1).source_message_id == "90"
        assert session.get(GroupBotAdmission, 2).source_message_id == ""


def test_standard_rule_rearms_current_follow_from_policy_unresolved_state():
    with _session() as session:
        group, accounts = _seed_global_rule_scope(session)
        _seed_waiting_admissions(session, group.id, accounts)
        first = session.get(GroupBotAdmission, 1)
        first.state = "group_bot_policy_unresolved"
        session.add(
            GroupBotRequiredChannelFollow(
                admission_id=first.id,
                channel_ref="school_news",
                source_message_id="old-control",
                action_id="old-follow-action",
                status="blocked",
                failure_code="group_bot_control_prompt_unverified",
            )
        )
        create_policy(
            session,
            tenant_id=1,
            group_id=group.id,
            completion_policy="explicit_bot_confirmation",
            trusted_bot_peer_id="900",
            reason="reviewed repeated group bot control",
            evidence_ref="group-context:100",
            created_by="operator",
        )
        for message_id, recipient in (("100", "unrelated-one"), ("101", "unrelated-two")):
            snapshot = _global_rule_snapshot()
            snapshot.content = f"{recipient}，您需要关注我们的频道才能发言。"
            snapshot.remote_message_id = message_id
            insert_context_snapshots(
                session,
                group,
                accounts[0],
                [snapshot],
                ignored_sender=lambda _snapshot: False,
                create_source_media=False,
                learning_scene=None,
            )

        follow = session.query(GroupBotRequiredChannelFollow).filter_by(
            admission_id=first.id,
            channel_ref="school_news",
        ).one()
        assert follow.status == "pending"
        assert follow.source_message_id == "101"
        assert follow.action_id != "old-follow-action"


def test_audited_global_callback_without_channel_reference_does_not_fan_out():
    with _session() as session:
        group, accounts = _seed_global_rule_scope(session)
        _seed_waiting_admissions(session, group.id, accounts)
        create_policy(
            session,
            tenant_id=1,
            group_id=group.id,
            completion_policy="explicit_bot_confirmation",
            trusted_bot_peer_id="900",
            reason="reviewed group bot control",
            evidence_ref="group-context:101",
            created_by="operator",
        )
        snapshot = _global_rule_snapshot()
        snapshot.content = "请点击下方按钮完成验证。"
        snapshot.remote_message_id = "101"
        snapshot.control_buttons = [{"row": 0, "col": 0, "text": "我已加入", "action_type": "callback"}]

        insert_context_snapshots(
            session,
            group,
            accounts[0],
            [snapshot],
            ignored_sender=lambda _snapshot: False,
            create_source_media=False,
            learning_scene=None,
        )

        assert session.query(Action).filter(Action.action_type == GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE).count() == 0
        assert session.get(GroupBotAdmission, 1).state == "group_bot_rule_unattributed"


def test_audited_global_prompt_rearms_old_unverified_current_channel_follow():
    with _session() as session:
        group, accounts = _seed_global_rule_scope(session)
        _seed_waiting_admissions(session, group.id, accounts)
        first = session.get(GroupBotAdmission, 1)
        session.add(
            GroupBotRequiredChannelFollow(
                admission_id=first.id,
                channel_ref="school_news",
                source_message_id="old-control",
                action_id="old-follow-action",
                status="blocked",
                failure_code="group_bot_control_prompt_unverified",
            )
        )
        create_policy(
            session,
            tenant_id=1,
            group_id=group.id,
            completion_policy="explicit_bot_confirmation",
            trusted_bot_peer_id="900",
            reason="reviewed new group bot control",
            evidence_ref="group-context:99",
            created_by="operator",
        )

        insert_context_snapshots(
            session,
            group,
            accounts[0],
            [_global_rule_snapshot()],
            ignored_sender=lambda _snapshot: False,
            create_source_media=False,
            learning_scene=None,
        )

        follow = session.query(GroupBotRequiredChannelFollow).filter_by(admission_id=first.id, channel_ref="school_news").one()
        assert follow.status == "pending"
        assert follow.failure_code == ""
        assert follow.source_message_id == "99"
        assert follow.action_id != "old-follow-action"
        follows = list(session.query(Action).filter(Action.action_type == GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE))
        assert {action.account_id for action in follows} == {11, 12}


def _seed_global_rule_scope(session: Session) -> tuple[TgGroup, list[TgAccount]]:
    group = TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="g", group_type="supergroup")
    accounts = [
        TgAccount(id=11, tenant_id=1, display_name="first", username="first", phone_masked="+101"),
        TgAccount(id=12, tenant_id=1, display_name="second", username="second", phone_masked="+102"),
    ]
    session.add_all(
        [
            Tenant(id=1, name="t"),
            group,
            *accounts,
            OperationTarget(id=8, tenant_id=1, target_type="group", tg_peer_id="-1007", title="g"),
            Task(
                id="task-ai",
                tenant_id=1,
                name="ai",
                type="group_ai_chat",
                status="running",
                type_config={"target_group_id": 7},
            ),
            TaskMembershipAdmissionItem(tenant_id=1, task_id="task-ai", account_id=11, target_id=8),
            TaskMembershipAdmissionItem(tenant_id=1, task_id="task-ai", account_id=12, target_id=8),
        ]
    )
    session.flush()
    return group, accounts


def _seed_waiting_admissions(session: Session, group_id: int, accounts: list[TgAccount]) -> None:
    for account in accounts:
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=group_id,
            account_id=account.id,
            membership_action_id=f"join-{account.id}",
            join_start_cursor="10",
        )
        admission.state = "group_bot_rule_unattributed"
        admission.failure_code = "group_bot_rule_unattributed"
    session.flush()


def _global_rule_snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        content="您需要关注我们的频道才能发言。",
        remote_message_id="99",
        sender_peer_id="900",
        sender_name="gatebot",
        sender_username="gatebot",
        is_bot=True,
        sender_role="unknown",
        message_type="text",
        sent_at=None,
        control_buttons=[
            {"row": 0, "col": 0, "text": "关注频道", "url": "https://t.me/school_news", "action_type": "url"},
            {"row": 1, "col": 0, "text": "我已加入", "action_type": "callback"},
        ],
    )


def select_admission(session: Session, *, group_id: int, account_id: int):
    from sqlalchemy import select

    return select(GroupBotAdmission).where(
        GroupBotAdmission.group_id == group_id,
        GroupBotAdmission.account_id == account_id,
    )


def test_dispatch_fairness_classifies_group_bot_follow_as_admission_retry():
    from app.services.task_center.dispatch_fairness import classify_action_payload

    # Unbound follow actions stay ordinary so they cannot starve search_join globally.
    assert classify_action_payload(GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE, {}, "group_ai_chat") == "ordinary"
    # Bound to same tenant+task+account admission → target_admission_retry tier (PRD §8.3).
    bound = {
        "admission_bound_task_id": "task-1",
        "admission_bound_account_id": 11,
    }
    assert (
        classify_action_payload(GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE, bound, "group_ai_chat")
        == "target_admission_retry"
    )
    assert (
        classify_action_payload("group_bot_control_observation", bound, "group_ai_chat")
        == "target_admission_retry"
    )
