from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services.task_center import comment_generation_pipeline
from app.services.task_center.comment_generation_pipeline import (
    CommentGenerationDependencies,
    GeneratedCommentResult,
)


pytestmark = pytest.mark.no_postgres


def test_provider_failure_closes_read_transaction_before_comment_retry(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    observed: list[str] = []

    def unavailable_after_lookup(session, _tenant_id, config, **_kwargs):
        assert session.in_transaction() is False
        session.execute(text("SELECT 1"))
        observed.append(str(config.get("_ai_fallback_stage") or "primary_m3"))
        raise RuntimeError("configured model unavailable")

    def emoji_fallback(session, *_args, **_kwargs):
        assert session.in_transaction() is False
        return GeneratedCommentResult("👍", 0, fallback_kind="emoji_text")

    monkeypatch.setattr(
        comment_generation_pipeline,
        "_emoji_fallback_result",
        emoji_fallback,
    )
    request = SimpleNamespace(
        tenant_id=1,
        config={},
        payload=SimpleNamespace(
            reply_to_message_id=0,
            message_content="频道正文",
            target_display="频道",
        ),
    )
    dependencies = CommentGenerationDependencies(
        direct_generator=unavailable_after_lookup,
        reply_generator=unavailable_after_lookup,
    )
    with Session(engine) as session:
        result = comment_generation_pipeline._run_generation_stages(
            session,
            request,
            dependencies,
            action_loader=lambda *_args: None,
        )

    assert observed == ["primary_m3"] * 3 + ["fallback_m25"] * 3
    assert result.fallback_kind == "emoji_text"
