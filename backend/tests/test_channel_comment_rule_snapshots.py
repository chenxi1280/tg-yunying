from __future__ import annotations

import pytest

from app.integrations.telegram import SendResult
from app.models import RuleSet, RuleSetVersion
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


def _dependencies() -> CommentGenerationDependencies:
    return CommentGenerationDependencies(
        direct_generator=lambda *_args, **_kwargs: (["这个引流细节挺清楚"], 1),
        reply_generator=_forbidden_generation,
    )


def _configure_gateway(monkeypatch, observed: list[str]) -> None:
    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())

    def send(_account_id, _channel_peer, _message_id, *args, **_kwargs):
        observed.append(args[0])
        return SendResult(True, remote_message_id="9902")

    monkeypatch.setattr(dispatcher.gateway, "reply_channel_message", send)


def _forbidden_generation(*_args, **_kwargs):
    pytest.fail("comment action must not use reply generation")


def _forbidden_gateway(*_args, **_kwargs):
    pytest.fail("invalid fixed rule snapshot must not enter Telegram gateway")
