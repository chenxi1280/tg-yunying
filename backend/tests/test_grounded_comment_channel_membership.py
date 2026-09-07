import pytest
from sqlalchemy import delete, select

from app.models import Action, ExecutionAttempt, OperationTarget, TgAccount, TgGroupAccount
from app.services.task_center import dispatcher
from app.services.task_center.channel_membership import mark_channel_membership_joined
from app.services.task_center.channel_payloads import PostCommentPayload
from app.services.task_center.executors.channel_comment_accounts import prepare_comment_accounts
from channel_comment_planner_test_support import planner_session, seed_comment_task


pytestmark = pytest.mark.no_postgres
CHANNEL_ID = 31
ACCOUNT_ID = 101


@pytest.mark.parametrize("grounded", [False, True])
def test_comment_candidates_require_channel_membership(grounded):
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment")
        task.type_config = {**task.type_config, "channel_comment_grounding_v1_enabled": grounded}
        session.execute(delete(TgGroupAccount).where(TgGroupAccount.account_id != ACCOUNT_ID))
        session.commit()
        setup = prepare_comment_accounts(
            session, task, session.get(OperationTarget, CHANNEL_ID), config=task.type_config,
        )
        assert [account.id for account in setup.accounts] == [ACCOUNT_ID]


@pytest.mark.parametrize("grounded", [False, True])
def test_grounded_candidates_do_not_require_channel_send_permission(grounded):
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment")
        task.type_config = {**task.type_config, "channel_comment_grounding_v1_enabled": grounded}
        for link in session.scalars(select(TgGroupAccount)):
            link.can_send = False
        session.commit()
        setup = prepare_comment_accounts(
            session, task, session.get(OperationTarget, CHANNEL_ID), config=task.type_config,
        )
        assert {account.id for account in setup.accounts} == ({101, 102, 103} if grounded else set())


def _queued_comment(session, task, *, grounded):
    payload = PostCommentPayload(
        channel_id="-10031", channel_target_id=CHANNEL_ID, message_id=9001,
        comment_text="测试评论",
        grounding_enrollment_id="enrollment-review" if grounded else "",
        grounding_snapshot_id="snapshot-review", comment_grounding_revision=1,
        grounding_evidence_hash="evidence-review", ai_generation_status="ready",
        comment_lifecycle_state="quality_accepted",
        discussion_group_binding_id="binding-review", discussion_group_identity_hash="group-review",
        discussion_thread_binding_id="thread-review", discussion_thread_identity_hash="thread-review",
        discussion_peer_id="-10032", thread_root_message_id=8001,
        rpc_mode="channel_comment_to", actual_target_peer="-10031", membership_fact_id="fact-review",
    )
    action = Action(
        id="queued-comment", tenant_id=1, task_id=task.id, task_type=task.type,
        action_type="post_comment", account_id=ACCOUNT_ID, status="pending",
        payload=payload.model_dump(mode="json"),
    )
    session.add(action)
    session.commit()
    return action, payload


@pytest.mark.parametrize("grounded", [False, True])
def test_removed_channel_membership_defers_without_comment_gateway(monkeypatch, grounded):
    def forbid_gateway(*args, **kwargs):
        pytest.fail("unfollowed account must not reach comment Gateway")

    monkeypatch.setattr(dispatcher, "_send_channel_comment", forbid_gateway)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment")
        action, payload = _queued_comment(session, task, grounded=grounded)
        session.execute(delete(TgGroupAccount).where(TgGroupAccount.account_id == ACCOUNT_ID))
        session.commit()
        context = dispatcher.CommentDispatchContext(
            account=session.get(TgAccount, ACCOUNT_ID), credentials=None, payload=payload,
        )
        for _ in range(2):
            dispatcher._dispatch_comment(session, action, context)
            session.flush()
        membership = session.scalars(select(Action).where(
            Action.action_type == "ensure_target_membership",
        )).one()
        assert action.status == "pending"
        assert action.result["error_code"] == "comment_membership_required"
        assert membership.account_id == ACCOUNT_ID
        assert membership.payload["channel_target_id"] == CHANNEL_ID
        assert membership.payload["require_send"] is (not grounded)
        assert session.scalar(select(ExecutionAttempt).where(
            ExecutionAttempt.action_id == action.id,
        )) is None


def test_grounded_membership_guard_resumes_after_channel_follow():
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment")
        action, _payload = _queued_comment(session, task, grounded=True)
        account = session.get(TgAccount, ACCOUNT_ID)
        session.execute(delete(TgGroupAccount).where(TgGroupAccount.account_id == ACCOUNT_ID))
        session.commit()
        assert not dispatcher._ensure_channel_action_membership(
            session, action, account=account, channel_target_id=CHANNEL_ID,
        )
        mark_channel_membership_joined(session, 1, CHANNEL_ID, ACCOUNT_ID)
        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.account_id == ACCOUNT_ID))
        link.can_send = False
        session.flush()
        assert dispatcher._ensure_channel_action_membership(
            session, action, account=account, channel_target_id=CHANNEL_ID,
        )
