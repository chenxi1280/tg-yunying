from __future__ import annotations

import pytest

from app.ai_gateway import AiDraftCandidate, AiGenerationResult, AiUsage
from app.integrations.telegram import SendResult
from app.models import Action, AiProvider, ChannelMessageComment, Task, TenantAiSetting, TgGroup
from app.security import encrypt_secret
from app.services.task_center import ai_generator, comment_generation_dispatch, dispatcher, service
from app.services.task_center.ai_generator import AiGenerationUnavailable
from app.services.task_center.ai_generation_recovery import recover_stale_pre_gateway_generation
from app.services.task_center.comment_generation_dispatch import CommentGenerationDependencies
from channel_comment_dispatch_test_support import (
    comment_dispatch_session,
    expire_comment_action,
    seed_dispatch_scope,
)


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("reply", [False, True])
def test_comment_generation_and_gateway_run_without_database_transaction(monkeypatch, reply: bool) -> None:
    observed = {"provider": 0, "gateway": 0}
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session, reply=reply)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "reply_channel_message",
            _gateway_sender(session, observed),
        )

        handled = dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=_dependencies(session, observed),
        )

        assert handled is True
        assert action.status == "success", action.result
        assert action.payload["ai_generation_status"] == "ready"
        assert action.payload["comment_text"] == ("引用真实回复" if reply else "真实读者评论")
        assert observed == {"provider": 1, "gateway": 1}


def test_production_comment_provider_call_runs_without_database_transaction(monkeypatch) -> None:
    observed = {"provider": 0, "gateway": 0}
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        _seed_ai_provider(session)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
        monkeypatch.setattr(dispatcher.gateway, "reply_channel_message", _gateway_sender(session, observed))

        def generate_drafts(*_args, **_kwargs):
            assert session.in_transaction() is False
            observed["provider"] += 1
            return AiGenerationResult(
                candidates=[AiDraftCandidate(persona="读者", content="河东区这个位置方便吗")],
                usage=AiUsage(total_tokens=6),
            )

        monkeypatch.setattr(ai_generator.ai_gateway, "generate_drafts", generate_drafts)

        assert dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=CommentGenerationDependencies(),
        ) is True

        assert action.status == "success"
        assert observed == {"provider": 1, "gateway": 1}


def test_invalid_reply_target_skips_generation_and_gateway_without_direct_downgrade(monkeypatch) -> None:
    observed = {"provider": 0, "gateway": 0}
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session, reply=True)
        session.delete(session.get(ChannelMessageComment, 51))
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
        monkeypatch.setattr(dispatcher.gateway, "reply_channel_message", _forbidden_gateway)

        assert dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=_dependencies(session, observed),
        ) is True

        assert action.status == "failed"
        assert action.result["error_code"] == "reply_target_missing"
        assert action.payload["ai_generation_status"] == "reply_target_missing"
        assert action.payload["comment_mode"] == "reply"
        assert action.payload["reply_to_message_id"] == 8101
        assert observed == {"provider": 0, "gateway": 0}


def test_stale_reply_target_skips_generation_and_gateway(monkeypatch) -> None:
    observed = {"provider": 0, "gateway": 0}
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session, reply=True)
        expire_comment_action(action)
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
        monkeypatch.setattr(dispatcher.gateway, "reply_channel_message", _forbidden_gateway)

        assert dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=_dependencies(session, observed),
        ) is True

        assert action.result["error_code"] == "reply_target_stale"
        assert observed == {"provider": 0, "gateway": 0}


def test_generation_failure_is_explicit_and_never_enters_gateway(monkeypatch) -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
        monkeypatch.setattr(dispatcher.gateway, "reply_channel_message", _forbidden_gateway)

        assert dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=CommentGenerationDependencies(
                direct_generator=_failed_generation,
                reply_generator=_failed_generation,
            ),
        ) is True

        assert action.status == "failed"
        assert action.payload["ai_generation_status"] == "generation_failed"
        assert action.result["error_code"] == "generation_failed"


def test_generated_comment_reuses_outbound_filter_before_gateway(monkeypatch) -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        session.get(TgGroup, 71).banned_words = "禁止词"
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
        monkeypatch.setattr(dispatcher.gateway, "reply_channel_message", _forbidden_gateway)

        assert dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=CommentGenerationDependencies(
                direct_generator=lambda *_args, **_kwargs: (["这里有禁止词"], 2),
                reply_generator=_forbidden_generation,
            ),
        ) is True

        assert action.status == "failed"
        assert action.payload["ai_generation_status"] == "content_rejected"
        assert action.result["error_code"] == "content_rejected"


def test_phase_c_commit_failure_is_generation_unknown_and_skips_gateway(monkeypatch) -> None:
    observed = {"provider": 0, "gateway": 0}
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
        monkeypatch.setattr(dispatcher.gateway, "reply_channel_message", _forbidden_gateway)

        assert dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=_dependencies(
                session,
                observed,
                phase_c_commit=_failed_phase_c_commit,
            ),
        ) is True

        refreshed = session.get(Action, action.id)
        assert refreshed.id == "comment-dispatch-action"
        assert refreshed.status == "pending"
        assert refreshed.payload["ai_generation_status"] == "ai_result_persist_unknown"
        assert refreshed.result["error_code"] == "ai_result_persist_unknown"
        assert observed == {"provider": 1, "gateway": 0}


def test_ready_comment_is_not_generated_again(monkeypatch) -> None:
    observed = {"provider": 0, "gateway": 0}
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        action.payload = {
            **action.payload,
            "comment_text": "已持久化评论",
            "ai_generation_status": "ready",
        }
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
        monkeypatch.setattr(dispatcher.gateway, "reply_channel_message", _gateway_sender(session, observed))

        assert dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=_dependencies(session, observed, forbid_generation=True),
        ) is True

        assert action.status == "success"
        assert action.payload["comment_text"] == "已持久化评论"
        assert observed == {"provider": 0, "gateway": 1}


def test_stale_generation_attempt_cannot_cas_ready() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        request = comment_generation_dispatch.prepare_comment_generation_request(
            session,
            action,
            session.get(Task, action.task_id),
        )
        action.payload = {**action.payload, "ai_generation_claim_token": "new-claim"}
        session.commit()

        with pytest.raises(comment_generation_dispatch.GenerationAttemptStale):
            comment_generation_dispatch.persist_comment_generation_result(
                session,
                request,
                "过期 worker 的结果",
                tokens=1,
            )

        assert session.get(Action, action.id).payload["ai_generation_status"] != "ready"


def test_lifetime_cap_completed_task_is_not_revived_by_recovery() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = session.get(Task, action.task_id)
        task.status = "completed"
        task.next_run_at = None
        task.stats = {"completion_reason": "lifetime_cap_reached"}
        session.commit()

        service._recover_continuous_task_states(session)

        assert task.status == "completed"
        assert task.next_run_at is None


def test_stale_comment_generation_after_provider_start_recovers_as_generation_unknown() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = session.get(Task, action.task_id)
        request = comment_generation_dispatch.prepare_comment_generation_request(session, action, task)
        comment_generation_dispatch._mark_provider_call_started(session, request)

        assert recover_stale_pre_gateway_generation(action) is True

        assert action.status == "pending"
        assert action.payload["ai_generation_status"] == "ai_result_persist_unknown"
        assert action.status != "unknown_after_send"


def _dependencies(session, observed, *, phase_c_commit=None, forbid_generation=False):
    generator = _forbidden_generation if forbid_generation else _direct_generator(session, observed)
    kwargs = {"direct_generator": generator, "reply_generator": _reply_generator(session, observed)}
    if phase_c_commit is not None:
        kwargs["phase_c_commit"] = phase_c_commit
    return CommentGenerationDependencies(**kwargs)


def _seed_ai_provider(session) -> None:
    session.add(AiProvider(
        id=1,
        provider_name="MiniMax",
        provider_type="openai_compatible",
        base_url="https://api.minimaxi.com/v1",
        model_name="MiniMax-M3",
        api_key_ciphertext=encrypt_secret("test-key"),
        health_status="健康",
    ))
    session.add(TenantAiSetting(
        tenant_id=1,
        default_provider_id=1,
        ai_enabled=True,
        max_tokens=1024,
    ))
    session.commit()


def _direct_generator(session, observed):
    def generate(*_args, **_kwargs):
        assert session.in_transaction() is False
        observed["provider"] += 1
        return ["真实读者评论"], 7

    return generate


def _reply_generator(session, observed):
    def generate(*_args, **_kwargs):
        assert session.in_transaction() is False
        observed["provider"] += 1
        return ["引用真实回复"], 8

    return generate


def _gateway_sender(session, observed):
    def send(*_args, **_kwargs):
        assert session.in_transaction() is False
        observed["gateway"] += 1
        return SendResult(True, remote_message_id="9901")

    return send


def _failed_generation(*_args, **_kwargs):
    raise AiGenerationUnavailable("provider unavailable")


def _failed_phase_c_commit(_session) -> None:
    raise RuntimeError("injected phase c commit failure")


def _forbidden_generation(*_args, **_kwargs):
    pytest.fail("ready comment must not call generation")


def _forbidden_gateway(*_args, **_kwargs):
    pytest.fail("invalid or unknown generation must not call Telegram")
