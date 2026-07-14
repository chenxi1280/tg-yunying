from __future__ import annotations

import pytest

from app.models import Action, ChannelMessageComment, RuleSetVersion
from app.services.task_center import dispatcher
from app.services.task_center.comment_generation_dispatch import CommentGenerationDependencies
from channel_comment_dispatch_test_support import comment_dispatch_session, seed_dispatch_scope


pytestmark = pytest.mark.no_postgres


def test_stale_comment_generation_releases_runtime_reservation_without_overwriting_new_claim(monkeypatch) -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        _reserve_local_runtime(action)
        _configure_no_gateway(monkeypatch)

        def lose_claim(*_args, **_kwargs):
            current = session.get(Action, action.id)
            current.payload = {
                **current.payload,
                "ai_generation_claim_owner": "dispatcher-new",
                "ai_generation_claim_token": "claim-new",
            }
            current.lease_owner = "dispatcher-new"
            session.commit()
            return ["河东区这个位置方便吗"], 1

        dependencies = CommentGenerationDependencies(
            direct_generator=lose_claim,
            reply_generator=_forbidden_external,
        )
        try:
            assert dispatcher.dispatch_action(
                session,
                action,
                comment_generation_dependencies=dependencies,
            ) is True

            current = session.get(Action, action.id)
            assert current.status == "executing"
            assert current.lease_owner == "dispatcher-new"
            assert current.payload["ai_generation_claim_token"] == "claim-new"
            assert action.id not in dispatcher._ACTION_RESERVATIONS
            assert action.account_id not in dispatcher._IN_FLIGHT_ACCOUNTS
        finally:
            _clear_local_runtime()


def test_prepare_stage_stale_claim_releases_runtime_reservation(monkeypatch) -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        action.payload = {**action.payload, "ai_generation_claim_token": ""}
        session.commit()
        _reserve_local_runtime(action)
        _configure_no_gateway(monkeypatch)
        try:
            assert dispatcher.dispatch_action(
                session,
                action,
                comment_generation_dependencies=_forbidden_dependencies(),
            ) is True

            assert action.status == "executing"
            assert action.id not in dispatcher._ACTION_RESERVATIONS
            assert action.account_id not in dispatcher._IN_FLIGHT_ACCOUNTS
        finally:
            _clear_local_runtime()


@pytest.mark.parametrize(
    ("scenario", "expected_outcome"),
    [
        ("reply_missing", "reply_target_missing"),
        ("generation_failed", "generation_failed"),
        ("quality_rejected", "rule_output_rejected"),
    ],
)
def test_terminal_generation_failures_finish_attempt_history(
    monkeypatch,
    scenario: str,
    expected_outcome: str,
) -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session, reply=scenario == "reply_missing")
        dependencies = _terminal_failure_scenario(session, action, scenario)
        _configure_no_gateway(monkeypatch)

        assert dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=dependencies,
        ) is True

        attempt = action.payload["ai_generation_attempt_history"][-1]
        assert action.status == "failed"
        assert attempt["outcome"] == expected_outcome
        assert attempt["finished_at"]


def test_new_attempt_replaces_stale_provider_marker_with_current_attempt(monkeypatch) -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        action.result = {
            "ai_provider_call_started_at": "2026-01-01T00:00:00",
            "ai_provider_call_attempt_id": "old-attempt",
        }
        session.commit()
        observed: dict[str, str] = {}
        _configure_success_gateway(monkeypatch)

        def generate(*_args, **_kwargs):
            current = session.get(Action, action.id)
            observed.update(current.result)
            observed["attempt_id"] = current.payload["ai_generation_attempt_id"]
            return ["河东区这个位置方便吗"], 1

        dependencies = CommentGenerationDependencies(
            direct_generator=generate,
            reply_generator=_forbidden_external,
        )
        assert dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=dependencies,
        ) is True

        assert observed["ai_provider_call_attempt_id"] == observed["attempt_id"]
        assert observed["ai_provider_call_attempt_id"] != "old-attempt"
        assert action.payload["ai_generation_attempt_history"][-1]["outcome"] == "ready"


def _terminal_failure_scenario(session, action: Action, scenario: str) -> CommentGenerationDependencies:
    if scenario == "reply_missing":
        session.delete(session.get(ChannelMessageComment, 51))
        session.commit()
        generator = _forbidden_external
    elif scenario == "generation_failed":
        generator = _raise_generation_failure
    else:
        version = session.get(RuleSetVersion, 62)
        version.output_checks = {"forbidden_keywords": ["引流"], "failure_strategy": "drop"}
        session.commit()
        generator = _quality_rejected_generation
    return CommentGenerationDependencies(
        direct_generator=generator,
        reply_generator=generator,
    )


def _reserve_local_runtime(action: Action) -> None:
    _clear_local_runtime()
    dispatcher._IN_FLIGHT_ACCOUNTS.add(action.account_id)
    dispatcher._ACTION_RESERVATIONS[action.id] = dispatcher._runtime_resources._RuntimeReservation(
        account_id=action.account_id,
    )


def _clear_local_runtime() -> None:
    dispatcher._ACTION_RESERVATIONS.clear()
    dispatcher._IN_FLIGHT_ACCOUNTS.clear()


def _forbidden_dependencies() -> CommentGenerationDependencies:
    return CommentGenerationDependencies(
        direct_generator=_forbidden_external,
        reply_generator=_forbidden_external,
    )


def _configure_no_gateway(monkeypatch) -> None:
    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
    monkeypatch.setattr(dispatcher.gateway, "reply_channel_message", _forbidden_external)


def _configure_success_gateway(monkeypatch) -> None:
    from app.integrations.telegram import SendResult

    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
    monkeypatch.setattr(
        dispatcher.gateway,
        "reply_channel_message",
        lambda *_args, **_kwargs: SendResult(True, remote_message_id="recovery-gap-success"),
    )


def _raise_generation_failure(*_args, **_kwargs):
    raise RuntimeError("provider failed")


def _quality_rejected_generation(*_args, **_kwargs):
    return ["这个引流细节挺清楚"], 1


def _forbidden_external(*_args, **_kwargs):
    pytest.fail("forbidden external call")
