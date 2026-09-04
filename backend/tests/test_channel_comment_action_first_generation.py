from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from app.integrations.telegram import SendResult
from app.ai_transport_errors import AiProviderResultUnknown
from app.services.antigravity_provider_client import AntigravityProviderResultUnknown
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.comment_generation_pipeline import CommentGenerationDependencies
from app.services.task_center.comment_generation_worker import drain_comment_generation
from channel_comment_dispatch_test_support import comment_dispatch_session, seed_dispatch_scope


pytestmark = pytest.mark.no_postgres


def test_dispatcher_never_generates_pending_comment(monkeypatch) -> None:
    calls = {"provider": 0, "gateway": 0}
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
        monkeypatch.setattr(
            dispatcher.gateway, "reply_channel_message",
            lambda *_args, **_kwargs: _count_gateway(calls),
        )

        assert dispatcher.dispatch_action(
            session,
            action,
            comment_generation_dependencies=_dependencies(calls),
        ) is True

        assert action.status == "pending"
        assert action.result["error_code"] == "comment_generation_worker_required"
        assert calls == {"provider": 0, "gateway": 0}


def test_generation_worker_freezes_content_before_dispatcher(monkeypatch) -> None:
    calls = {"provider": 0, "gateway": 0}
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        engine = session.get_bind()
        _release_seeded_dispatch_claim(action)
        session.commit()

        processed = drain_comment_generation(
            lambda: Session(bind=engine),
            limit=1,
            dependencies=_dependencies(calls),
        )

        session.expire_all()
        action = session.get(type(action), action.id)
        assert processed == 1
        assert action.status == "pending"
        assert action.payload["ai_generation_status"] == "ready"
        assert action.payload["comment_lifecycle_state"] == "quality_accepted"
        assert action.payload["comment_text"] == "真实读者评论"
        assert calls == {"provider": 1, "gateway": 0}

        _claim_for_dispatch(action)
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "reply_channel_message",
            lambda *_args, **_kwargs: _count_gateway(calls),
        )

        assert dispatcher.dispatch_action(session, action) is True
        assert action.status == "success"
        assert calls == {"provider": 1, "gateway": 1}


@pytest.mark.parametrize("error_type", (AiProviderResultUnknown, AntigravityProviderResultUnknown))
def test_provider_unknown_is_fenced_and_not_called_again(error_type) -> None:
    calls = {"provider": 0}

    def unknown(*_args, **_kwargs):
        calls["provider"] += 1
        raise error_type("provider_result_unknown")

    dependencies = CommentGenerationDependencies(
        direct_generator=unknown,
        reply_generator=unknown,
    )
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        engine = session.get_bind()
        _release_seeded_dispatch_claim(action)
        session.commit()

        assert drain_comment_generation(
            lambda: Session(bind=engine), limit=1, dependencies=dependencies,
        ) == 1
        assert drain_comment_generation(
            lambda: Session(bind=engine), limit=1, dependencies=dependencies,
        ) == 0

        session.expire_all()
        action = session.get(type(action), action.id)
        assert action.status == "pending"
        assert action.payload["ai_generation_status"] == "provider_result_unknown"
        assert action.payload["comment_lifecycle_state"] == "provider_result_unknown"
        assert calls == {"provider": 1}


def _dependencies(calls: dict[str, int]) -> CommentGenerationDependencies:
    def generate(*_args, **_kwargs):
        calls["provider"] += 1
        return ["真实读者评论"], 1

    return CommentGenerationDependencies(
        direct_generator=generate,
        reply_generator=generate,
    )


def _count_gateway(calls: dict[str, int]) -> SendResult:
    calls["gateway"] += 1
    return SendResult(True, remote_message_id="9901")


def _release_seeded_dispatch_claim(action) -> None:
    action.status = "pending"
    action.lease_owner = ""
    action.lease_expires_at = None
    action.claim_owner = ""
    action.claim_token = ""


def _claim_for_dispatch(action) -> None:
    action.status = "executing"
    action.lease_owner = "dispatcher-test"
    action.lease_expires_at = _now() + timedelta(minutes=5)
