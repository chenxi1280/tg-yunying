from __future__ import annotations

from datetime import timedelta

import pytest

from app.integrations.telegram import SendResult
from app.models import Action, ChannelMessage, ChannelMessageComment, RuleSet, RuleSetVersion, Task
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.comment_generation_dispatch import CommentGenerationDependencies
from channel_comment_dispatch_test_support import comment_dispatch_session, seed_dispatch_scope


pytestmark = pytest.mark.no_postgres


def test_phase_c_uses_archived_fixed_rule_snapshot_after_new_version_publish(monkeypatch) -> None:
    observed: list[str] = []
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        version = session.get(RuleSetVersion, 62)
        version.status = "archived"
        version.output_checks = {
            "forbidden_keywords": ["引流"],
            "failure_strategy": "transform_once_drop",
        }
        version.transforms = {"keyword_replacements": {"引流": "活动"}}
        session.add(_published_replacement_version())
        session.get(RuleSet, 61).active_version_id = 63
        session.commit()
        _configure_gateway(monkeypatch, observed)

        assert dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=_dependencies(),
        ) is True

        assert action.status == "success", action.result
        assert action.payload["comment_text"] == "这个活动细节挺清楚"
        assert observed == ["这个活动细节挺清楚"]


@pytest.mark.parametrize(
    ("status", "payload_patch"),
    [
        ("draft", {}),
        ("published", {"rule_set_version": 999}),
        ("published", {"rule_set_version_id": 63}),
    ],
)
def test_phase_c_rejects_rule_version_that_cannot_match_fixed_snapshot(
    monkeypatch,
    status: str,
    payload_patch: dict,
) -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        session.get(RuleSetVersion, 62).status = status
        action.payload = {**action.payload, **payload_patch}
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
        monkeypatch.setattr(dispatcher.gateway, "reply_channel_message", _forbidden_gateway)

        assert dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=_dependencies(),
        ) is True

        assert action.status == "failed"
        assert action.payload["ai_generation_status"] == "rule_version_unavailable"
        assert action.result["error_code"] == "rule_version_unavailable"


@pytest.mark.parametrize("target_state", ["missing", "closed"])
def test_direct_comment_unavailable_before_generation_skips_provider_and_gateway(
    monkeypatch,
    target_state: str,
) -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        _make_comment_target_unavailable(session, target_state)
        _configure_forbidden_external_calls(monkeypatch)

        assert dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=_forbidden_dependencies(),
        ) is True

        assert action.status == "failed"
        assert action.result["error_code"] == "comment_unavailable_message"


def test_phase_c_rechecks_comment_available_after_provider(monkeypatch) -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        _configure_forbidden_gateway(monkeypatch)

        def generate(*_args, **_kwargs):
            session.get(ChannelMessage, 41).comment_available = False
            session.commit()
            return ["河东区这个位置方便吗"], 1

        dependencies = CommentGenerationDependencies(
            direct_generator=generate,
            reply_generator=_forbidden_generation,
        )
        assert dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=dependencies,
        ) is True

        assert action.status == "failed"
        assert action.result["error_code"] == "comment_unavailable_message"


def test_empty_pending_blueprints_do_not_hide_older_success_duplicate(monkeypatch) -> None:
    duplicate = "河东区这个位置方便吗"
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = session.get(Task, action.task_id)
        task.type_config = {**task.type_config, "max_total_comments": 100}
        session.add(_historical_success(action, duplicate))
        session.add_all(_empty_pending_blueprint(action, index) for index in range(50))
        session.commit()
        _configure_forbidden_gateway(monkeypatch)

        assert dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=_dependencies(duplicate),
        ) is True

        assert action.status == "failed"
        assert action.result["error_code"] == "duplicate_rejected"


def test_legacy_message_id_history_still_rejects_duplicate(monkeypatch) -> None:
    duplicate = "河东区这个位置方便吗"
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        historical = _historical_success(action, duplicate)
        historical.payload = {key: value for key, value in historical.payload.items() if key != "channel_message_id"}
        session.add(historical)
        session.commit()
        _configure_forbidden_gateway(monkeypatch)

        assert dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=_dependencies(duplicate),
        ) is True

        assert action.status == "failed"
        assert action.result["error_code"] == "duplicate_rejected"


def _published_replacement_version() -> RuleSetVersion:
    return RuleSetVersion(
        id=63,
        tenant_id=1,
        rule_set_id=61,
        version=2,
        status="published",
        output_checks={"forbidden_keywords": ["引流"], "failure_strategy": "drop"},
        transforms={},
    )


def _dependencies(content: str = "这个引流细节挺清楚") -> CommentGenerationDependencies:
    return CommentGenerationDependencies(
        direct_generator=lambda *_args, **_kwargs: ([content], 1),
        reply_generator=_forbidden_generation,
    )


def _forbidden_dependencies() -> CommentGenerationDependencies:
    return CommentGenerationDependencies(
        direct_generator=_forbidden_generation,
        reply_generator=_forbidden_generation,
    )


def _configure_gateway(monkeypatch, observed: list[str]) -> None:
    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())

    def send(_account_id, _channel_peer, _message_id, *args, **_kwargs):
        observed.append(args[0])
        return SendResult(True, remote_message_id="9902")

    monkeypatch.setattr(dispatcher.gateway, "reply_channel_message", send)


def _configure_forbidden_gateway(monkeypatch) -> None:
    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
    monkeypatch.setattr(dispatcher.gateway, "reply_channel_message", _forbidden_gateway)


def _configure_forbidden_external_calls(monkeypatch) -> None:
    _configure_forbidden_gateway(monkeypatch)


def _make_comment_target_unavailable(session, target_state: str) -> None:
    if target_state == "closed":
        session.get(ChannelMessage, 41).comment_available = False
    else:
        session.delete(session.get(ChannelMessageComment, 51))
        session.delete(session.get(ChannelMessage, 41))
    session.commit()


def _historical_success(current: Action, content: str) -> Action:
    return _comment_history_action(
        current,
        action_id="historical-success",
        status="success",
        content=content,
        days=-1,
    )


def _empty_pending_blueprint(current: Action, index: int) -> Action:
    return _comment_history_action(
        current,
        action_id=f"empty-blueprint-{index}",
        status="pending",
        content="",
        days=0,
    )


def _comment_history_action(
    current: Action,
    *,
    action_id: str,
    status: str,
    content: str,
    days: int,
) -> Action:
    return Action(
        id=action_id,
        tenant_id=current.tenant_id,
        task_id=current.task_id,
        task_type="channel_comment",
        action_type="post_comment",
        account_id=current.account_id,
        status=status,
        created_at=_now() + timedelta(days=days),
        payload={
            "channel_target_id": 31,
            "channel_message_id": 41,
            "message_id": 9001,
            "comment_text": content,
        },
    )


def _forbidden_generation(*_args, **_kwargs):
    pytest.fail("comment action must not use reply generation")


def _forbidden_gateway(*_args, **_kwargs):
    pytest.fail("invalid fixed rule snapshot must not enter Telegram gateway")
