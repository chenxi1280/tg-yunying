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


GROUNDING_FIELDS = (
    "grounding_snapshot_id", "grounding_assignment_id", "grounding_primary_evidence_id",
    "grounding_primary_aspect_code", "grounding_primary_aspect_text", "grounding_speech_act",
)


def _grounding_request():
    return SimpleNamespace(
        tenant_id=1, config={"channel_comment_grounding_v1_enabled": True},
        payload=SimpleNamespace(
            **{field: "test-value" for field in GROUNDING_FIELDS},
            reply_to_message_id=0, message_content="测试通知", target_display="测试频道",
        ),
    )


@pytest.mark.parametrize("missing_field", GROUNDING_FIELDS)
def test_incomplete_grounding_is_blocked_without_provider_retry_or_fallback(
    monkeypatch, missing_field,
):
    request = _grounding_request()
    setattr(request.payload, missing_field, "")

    def unexpected_call(*_args, **_kwargs):
        pytest.fail("incomplete grounding must not call Provider or fallback")

    monkeypatch.setattr(comment_generation_pipeline, "_emoji_fallback_result", unexpected_call)
    dependencies = CommentGenerationDependencies(
        direct_generator=unexpected_call, reply_generator=unexpected_call,
    )
    engine = create_engine("sqlite:///:memory:")
    with Session(engine) as session:
        with pytest.raises(comment_generation_pipeline.CommentGenerationBlocked) as error:
            comment_generation_pipeline._run_generation_stages(
                session, request, dependencies, action_loader=unexpected_call,
            )
    engine.dispose()
    assert error.value.code == "channel_comment_grounding_assignment_incomplete"


def test_complete_grounding_reaches_generator_with_original_assignment():
    request = _grounding_request()
    calls = []

    def generate(_session, _tenant_id, config, **_kwargs):
        calls.append(config["_comment_grounding_assignment"])
        return ["收到通知"], 3

    result = comment_generation_pipeline._call_generator(
        None, request, CommentGenerationDependencies(direct_generator=generate),
        stage="primary_m3",
    )
    assert result == (["收到通知"], 3)
    assert len(calls) == 1
    assert calls[0]["snapshot_id"] == request.payload.grounding_snapshot_id
    assert calls[0]["assignment_id"] == request.payload.grounding_assignment_id
    assert calls[0]["relation_kind"] == "direct"


def test_dispatch_passes_incomplete_grounding_code_to_failure_persistence(monkeypatch):
    from app.services.task_center import comment_generation_dispatch as dispatch
    from app.services.task_center.ai_generator import AiGenerationUnavailable

    request = _grounding_request()
    request.has_cached_result = False
    request.payload.comment_fallback_intent_kind = ""
    request.payload.grounding_primary_evidence_id = ""
    persisted = []
    monkeypatch.setattr(dispatch, "_mark_provider_call_started", lambda *_args: None)
    monkeypatch.setattr(comment_generation_pipeline, "_comment_mask_fallback_reason",
        lambda *_args: "")
    monkeypatch.setattr(comment_generation_pipeline, "two_stage_enabled", lambda *_args: False)
    monkeypatch.setattr(dispatch, "_persist_generation_failure",
        lambda *_args, **kwargs: persisted.append(kwargs))

    def unexpected_provider(*_args, **_kwargs):
        pytest.fail("missing grounding must fail before Provider")

    engine = create_engine("sqlite:///:memory:")
    with Session(engine) as session:
        with pytest.raises(AiGenerationUnavailable,
                match="channel_comment_grounding_assignment_incomplete"):
            dispatch._generate_comment(session, request,
                CommentGenerationDependencies(direct_generator=unexpected_provider))
    engine.dispose()
    assert len(persisted) == 1
    assert persisted[0]["code"] == "channel_comment_grounding_assignment_incomplete"
