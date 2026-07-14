from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from sqlalchemy import delete

from app.database import Base, SessionLocal, engine
from app.integrations.telegram import SendResult
from app.models import (
    AccountStatus,
    Action,
    ChannelMessage,
    ChannelMessageComment,
    ExecutionAttempt,
    OperationTarget,
    RuleSet,
    RuleSetVersion,
    SchedulingSetting,
    Task,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.services._common import _now
from app.services.task_center import comment_generation_dispatch, dispatcher
from app.services.task_center.comment_generation_dispatch import (
    CommentGenerationDependencies,
    GenerationAttemptStale,
)
from app.services.task_center.comment_generation_quality import CommentQualityDecision


TENANT_ID = 915_715
TASK_ID = "pg-channel-comment-dispatch"
ACTION_ID = "pg-channel-comment-dispatch-action"
ACCOUNT_ID = TENANT_ID + 1
CHANNEL_ID = TENANT_ID + 2
MESSAGE_ID = TENANT_ID + 3
GROUP_ID = TENANT_ID + 4
RULE_SET_ID = TENANT_ID + 5
RULE_VERSION_ID = TENANT_ID + 6
COMMENT_ID = TENANT_ID + 7


def test_postgres_two_dispatchers_claim_and_generate_comment_once(monkeypatch) -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    start = Barrier(2)
    calls = {"provider": 0, "gateway": 0}
    lock = Lock()
    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
    monkeypatch.setattr(
        dispatcher.gateway,
        "reply_channel_message",
        _gateway_sender(calls, lock),
    )
    try:
        _seed_scope()

        def run_dispatcher(worker_id: str) -> int:
            start.wait(timeout=5)
            with SessionLocal() as session:
                claimed = dispatcher.claim_actions(session, limit=1, worker_id=worker_id)
                for action in claimed:
                    dispatcher.dispatch_action(
                        session,
                        action,
                        comment_generation_dependencies=_dependencies(session, calls, lock),
                    )
                session.commit()
                return len(claimed)

        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed_counts = list(pool.map(run_dispatcher, ("dispatcher-a", "dispatcher-b")))

        with SessionLocal() as session:
            action = session.get(Action, ACTION_ID)
            assert sorted(claimed_counts) == [0, 1]
            assert calls == {"provider": 1, "gateway": 1}
            assert action.status == "success"
            assert action.payload["ai_generation_status"] == "ready"
            assert action.payload["comment_text"] == "PG 真实评论"
    finally:
        _cleanup()


def test_postgres_comment_generation_cas_rejects_worker_losing_token_after_quality(monkeypatch) -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        _seed_scope()
        with SessionLocal() as session:
            action = session.get(Action, ACTION_ID)
            action.status = "executing"
            action.lease_owner = "dispatcher-old"
            action.payload = {
                **action.payload,
                "ai_generation_claim_owner": "dispatcher-old",
                "ai_generation_claim_token": "claim-old",
            }
            session.commit()
            request = comment_generation_dispatch.prepare_comment_generation_request(
                session,
                action,
                session.get(Task, TASK_ID),
            )

            def lose_token(*_args, **_kwargs):
                with SessionLocal() as contender:
                    current = contender.get(Action, ACTION_ID)
                    current.payload = {
                        **current.payload,
                        "ai_generation_claim_token": "claim-new",
                    }
                    contender.commit()
                return CommentQualityDecision(True, "PG CAS 评论")

            monkeypatch.setattr(comment_generation_dispatch, "evaluate_comment_generation_quality", lose_token)

            with pytest.raises(GenerationAttemptStale):
                comment_generation_dispatch.persist_comment_generation_result(
                    session,
                    request,
                    "PG CAS 评论",
                    tokens=1,
                )

        with SessionLocal() as session:
            assert session.get(Action, ACTION_ID).payload["ai_generation_status"] != "ready"
    finally:
        _cleanup()


def test_postgres_reply_comment_uses_persisted_target_and_reply_generator(monkeypatch) -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    calls = {"direct": 0, "reply": 0, "gateway": 0}
    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
    monkeypatch.setattr(dispatcher.gateway, "reply_channel_message", _counting_gateway(calls))
    try:
        _seed_scope()
        _seed_reply_target()
        with SessionLocal() as session:
            claimed = dispatcher.claim_actions(session, limit=1, worker_id="reply-worker")
            assert len(claimed) == 1
            dispatcher.dispatch_action(
                session,
                claimed[0],
                comment_generation_dependencies=_reply_dependencies(session, calls),
            )
            session.commit()

        with SessionLocal() as session:
            action = session.get(Action, ACTION_ID)
            assert action.status == "success"
            assert action.payload["comment_text"] == "PG 引用回复"
            assert calls == {"direct": 0, "reply": 1, "gateway": 1}
    finally:
        _cleanup()


def test_postgres_unknown_result_second_claim_reuses_cache_without_provider(monkeypatch) -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    calls = {"provider": 0, "gateway": 0}
    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
    monkeypatch.setattr(dispatcher.gateway, "reply_channel_message", _gateway_sender(calls, Lock()))
    try:
        _seed_scope()
        with SessionLocal() as session:
            first = dispatcher.claim_actions(session, limit=1, worker_id="unknown-worker")[0]
            dispatcher.dispatch_action(
                session,
                first,
                comment_generation_dependencies=_unknown_dependencies(session, calls),
            )
            session.commit()

        with SessionLocal() as session:
            pending = session.get(Action, ACTION_ID)
            assert pending.status == "pending"
            assert pending.payload["ai_generation_status"] == "ai_result_persist_unknown"
            assert pending.payload["ai_generation_result_cache"]["content"] == "PG 缓存评论"
            second = dispatcher.claim_actions(session, limit=1, worker_id="cache-worker")[0]
            dispatcher.dispatch_action(
                session,
                second,
                comment_generation_dependencies=CommentGenerationDependencies(
                    direct_generator=_forbidden_provider,
                    reply_generator=_forbidden_provider,
                ),
            )
            session.commit()

        with SessionLocal() as session:
            action = session.get(Action, ACTION_ID)
            outcomes = [item["outcome"] for item in action.payload["ai_generation_attempt_history"]]
            assert action.status == "success"
            assert action.payload["comment_text"] == "PG 缓存评论"
            assert action.payload["ai_generation_result_cache"] == {}
            assert outcomes == ["ai_result_persist_unknown", "ready"]
            assert calls == {"provider": 1, "gateway": 1}
    finally:
        _cleanup()


def _dependencies(session, calls, lock) -> CommentGenerationDependencies:
    def generate(*_args, **_kwargs):
        assert session.in_transaction() is False
        with lock:
            calls["provider"] += 1
        return ["PG 真实评论"], 3

    return CommentGenerationDependencies(
        direct_generator=generate,
        reply_generator=generate,
    )


def _gateway_sender(calls, lock):
    def send(*_args, **_kwargs):
        with lock:
            calls["gateway"] += 1
        return SendResult(True, remote_message_id="pg-comment-remote-id")

    return send


def _counting_gateway(calls):
    def send(*_args, **_kwargs):
        calls["gateway"] += 1
        return SendResult(True, remote_message_id="pg-reply-remote-id")

    return send


def _reply_dependencies(session, calls) -> CommentGenerationDependencies:
    def direct(*_args, **_kwargs):
        calls["direct"] += 1
        pytest.fail("reply action must not use direct generation")

    def reply(*_args, **_kwargs):
        assert session.in_transaction() is False
        calls["reply"] += 1
        return ["PG 引用回复"], 2

    return CommentGenerationDependencies(direct_generator=direct, reply_generator=reply)


def _unknown_dependencies(session, calls) -> CommentGenerationDependencies:
    def generate(*_args, **_kwargs):
        assert session.in_transaction() is False
        calls["provider"] += 1
        return ["PG 缓存评论"], 2

    def fail_commit(_session):
        raise RuntimeError("injected phase c commit failure")

    return CommentGenerationDependencies(
        direct_generator=generate,
        reply_generator=generate,
        phase_c_commit=fail_commit,
    )


def _forbidden_provider(*_args, **_kwargs):
    pytest.fail("cached retry must not call provider")


def _seed_scope() -> None:
    with SessionLocal() as session:
        session.add(Tenant(id=TENANT_ID, name="PG 评论 Dispatcher"))
        session.flush()
        _seed_rule(session)
        _seed_target(session)
        session.flush()
        session.add(ChannelMessage(
            id=MESSAGE_ID,
            tenant_id=TENANT_ID,
            channel_target_id=CHANNEL_ID,
            message_id=9001,
            content_preview="PG 频道消息",
            comment_available=True,
        ))
        session.add(_account())
        session.flush()
        session.add(TgGroupAccount(
            tenant_id=TENANT_ID,
            group_id=GROUP_ID,
            account_id=ACCOUNT_ID,
            can_send=True,
        ))
        session.add(_task())
        session.flush()
        session.add(_action())
        session.commit()


def _seed_reply_target() -> None:
    with SessionLocal() as session:
        session.add(ChannelMessageComment(
            id=COMMENT_ID,
            tenant_id=TENANT_ID,
            channel_target_id=CHANNEL_ID,
            channel_message_id=MESSAGE_ID,
            comment_message_id=8101,
            author_name="PG 读者",
            content_preview="PG 评论问题",
        ))
        action = session.get(Action, ACTION_ID)
        action.payload = {
            **action.payload,
            "comment_mode": "reply",
            "reply_to_message_id": 8101,
            "reply_target_author": "PG 读者",
            "reply_target_preview": "PG 评论问题",
            "reply_target_source": "persisted",
        }
        session.commit()


def _seed_rule(session) -> None:
    session.add(RuleSet(
        id=RULE_SET_ID,
        tenant_id=TENANT_ID,
        name="PG 评论规则",
        status="active",
        task_types=["channel_comment"],
    ))
    session.flush()
    session.add(RuleSetVersion(
        id=RULE_VERSION_ID,
        tenant_id=TENANT_ID,
        rule_set_id=RULE_SET_ID,
        version=1,
        status="published",
        output_checks={},
        transforms={},
    ))


def _seed_target(session) -> None:
    session.add(OperationTarget(
        id=CHANNEL_ID,
        tenant_id=TENANT_ID,
        target_type="channel",
        tg_peer_id=f"-100{CHANNEL_ID}",
        title="PG 评论频道",
        can_send=True,
        auth_status="已授权运营",
    ))
    session.add(TgGroup(
        id=GROUP_ID,
        tenant_id=TENANT_ID,
        tg_peer_id=f"-100{CHANNEL_ID}",
        title="PG 评论讨论组",
        auth_status="已授权运营",
    ))


def _account() -> TgAccount:
    return TgAccount(
        id=ACCOUNT_ID,
        tenant_id=TENANT_ID,
        display_name="PG 评论账号",
        username="pg_comment_dispatch",
        tg_first_name="评论号",
        avatar_object_key="avatars/pg-comment.jpg",
        profile_sync_status="已同步",
        phone_masked=str(ACCOUNT_ID),
        status=AccountStatus.ACTIVE.value,
        health_score=100,
        session_ciphertext="pg-comment-session",
    )


def _task() -> Task:
    return Task(
        id=TASK_ID,
        tenant_id=TENANT_ID,
        name="PG 评论 Dispatcher",
        type="channel_comment",
        status="running",
        account_config={"selection_mode": "all", "max_concurrent": 1},
        pacing_config={"mode": "fixed", "max_actions_per_hour": 10},
        type_config={
            "target_channel_id": CHANNEL_ID,
            "target_comments_per_message": 1,
            "max_total_comments": 10,
            "max_total_comments_jitter": 0,
            "rule_set_version_id": RULE_VERSION_ID,
        },
        stats={},
    )


def _action() -> Action:
    return Action(
        id=ACTION_ID,
        tenant_id=TENANT_ID,
        task_id=TASK_ID,
        task_type="channel_comment",
        action_type="post_comment",
        account_id=ACCOUNT_ID,
        status="pending",
        scheduled_at=_now(),
        payload={
            "channel_id": f"-100{CHANNEL_ID}",
            "channel_target_id": CHANNEL_ID,
            "channel_message_id": MESSAGE_ID,
            "message_id": 9001,
            "target_display": "PG 评论频道",
            "message_content": "PG 频道消息",
            "comment_text": "",
            "comment_mode": "comment",
            "slot_id": f"channel-comment:{MESSAGE_ID}:0",
            "ai_generation_id": f"{TASK_ID}:channel-comment:{MESSAGE_ID}:0",
            "ai_generation_status": "pending",
            "rule_set_id": RULE_SET_ID,
            "rule_set_version_id": RULE_VERSION_ID,
            "resolved_rule_set_version_id": RULE_VERSION_ID,
            "rule_set_version": 1,
        },
    )


def _cleanup() -> None:
    with SessionLocal() as session:
        session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.action_id == ACTION_ID))
        session.execute(delete(Action).where(Action.task_id == TASK_ID))
        session.execute(delete(Task).where(Task.id == TASK_ID))
        session.execute(delete(TgGroupAccount).where(TgGroupAccount.tenant_id == TENANT_ID))
        session.execute(delete(ChannelMessageComment).where(ChannelMessageComment.tenant_id == TENANT_ID))
        session.execute(delete(ChannelMessage).where(ChannelMessage.tenant_id == TENANT_ID))
        session.execute(delete(OperationTarget).where(OperationTarget.tenant_id == TENANT_ID))
        session.execute(delete(TgGroup).where(TgGroup.tenant_id == TENANT_ID))
        session.execute(delete(TgAccount).where(TgAccount.tenant_id == TENANT_ID))
        session.execute(delete(SchedulingSetting).where(SchedulingSetting.tenant_id == TENANT_ID))
        session.execute(delete(RuleSetVersion).where(RuleSetVersion.tenant_id == TENANT_ID))
        session.execute(delete(RuleSet).where(RuleSet.tenant_id == TENANT_ID))
        session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        session.commit()
