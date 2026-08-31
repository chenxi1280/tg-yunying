from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services.task_center import comment_generation_pipeline
from app.services.task_center.comment_generation_pipeline import (
    COMMENT_EMOJI_FALLBACKS,
    UNICODE_EMOJI_ALLOWLIST_V2,
    CommentGenerationDependencies,
    GeneratedCommentResult,
    _ordered_fallback_emojis,
)


pytestmark = pytest.mark.no_postgres


def test_comment_fallbacks_remain_emoji_only() -> None:
    assert COMMENT_EMOJI_FALLBACKS == ("👍", "🙂", "👏")
    assert len(UNICODE_EMOJI_ALLOWLIST_V2) == 20
    assert UNICODE_EMOJI_ALLOWLIST_V2 == (
        "👍", "🙂", "👏", "🔥", "❤️", "😍", "🤩", "🎉", "💯", "🙌",
        "👌", "✨", "😄", "😊", "🥳", "👀", "🤝", "💪", "🌟", "💖",
    )


def test_ordered_fallback_emojis_selection():
    req_v1 = SimpleNamespace(
        task_id=10,
        config={},
        payload=SimpleNamespace(channel_message_id=100, slot_id=1),
    )
    ordered_v1 = _ordered_fallback_emojis(req_v1)
    assert len(ordered_v1) == 3
    assert set(ordered_v1) == set(COMMENT_EMOJI_FALLBACKS)

    req_v2 = SimpleNamespace(
        task_id=10,
        config={"channel_comment_grounding_v1_enabled": True},
        payload=SimpleNamespace(channel_message_id=100, slot_id=1),
    )
    ordered_v2 = _ordered_fallback_emojis(req_v2)
    assert len(ordered_v2) == 20
    assert set(ordered_v2) == set(UNICODE_EMOJI_ALLOWLIST_V2)


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


def test_cached_image_fallback_replays_without_provider_or_mask_recheck() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = SimpleNamespace(
        has_cached_result=True,
        cached_content="",
        cached_tokens=0,
        cached_fallback_kind="image_meme",
        cached_fallback_reason="quality_exhausted",
        cached_attempts=(),
        cached_media_segment={"source": "tg-cache://-1001/88"},
        cached_selection_metadata={"selection_id": "selection-1"},
    )
    def provider_called(*_args, **_kwargs):
        pytest.fail("cached image fallback must not call provider")

    with Session(engine) as session:
        result = comment_generation_pipeline.generate_comment_result(
            session,
            request,
            CommentGenerationDependencies(
                direct_generator=provider_called,
                reply_generator=provider_called,
            ),
            action_loader=lambda *_args: pytest.fail(
                "cached image fallback must not reload mask/action"
            ),
        )

    assert result.fallback_kind == "image_meme"
    assert result.media_segment == request.cached_media_segment
    assert result.selection_metadata == request.cached_selection_metadata


def test_planned_fallback_skips_normal_generation(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    expected = GeneratedCommentResult("🔥", 0, fallback_kind="unicode_emoji")
    monkeypatch.setattr(
        comment_generation_pipeline,
        "_emoji_fallback_result",
        lambda *_args, **_kwargs: expected,
    )
    request = SimpleNamespace(
        has_cached_result=False,
        payload=SimpleNamespace(
            comment_fallback_intent_kind="planned",
            grounding_assignment_id="",
        ),
    )
    def provider_called(*_args, **_kwargs):
        pytest.fail("planned fallback must not call the normal generator")

    with Session(engine) as session:
        result = comment_generation_pipeline.generate_comment_result(
            session,
            request,
            CommentGenerationDependencies(
                direct_generator=provider_called,
                reply_generator=provider_called,
            ),
            action_loader=lambda *_args: None,
        )

    assert result is expected
