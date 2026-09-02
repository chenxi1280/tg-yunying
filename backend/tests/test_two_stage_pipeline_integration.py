from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import AiAccountVoiceProfile
from app.services.task_center import comment_generation_pipeline
from app.services.task_center.ai_generation_dependencies import GenerationDependencies
from app.services.task_center.ai_generation_pipeline import generate_quality_results
from app.services.task_center.ai_generator import AiGenerationUnavailable
from app.services.task_center.comment_generation_pipeline import CommentGenerationBlocked
from app.services.task_center.two_stage_generation import QUALITY_WAIT
from ai_generation_quality_test_support import _quantity_slot, _request
from two_stage_generation_test_support import (
    brief_payload,
    planner_factory,
    realizer_factory,
    reviewer_factory,
)


pytestmark = pytest.mark.no_postgres


def _sqlite_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    AiAccountVoiceProfile.__table__.create(engine)
    return engine


def _two_stage_request(**config_updates):
    config = {
        "ai_two_stage_enabled": True,
        "generation_slots": [_quantity_slot("slot-1", 11)],
        **config_updates,
    }
    return _request("", cached=False, config=config)


def _two_stage_dependencies(planner, realizer) -> GenerationDependencies:
    return GenerationDependencies(
        normal_generator=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("legacy path must not run")),
        reply_generator=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("legacy path must not run")),
        reply_target_probe=lambda *_a, **_k: None,
        reply_message_fetcher=lambda *_a, **_k: None,
        brief_planner=planner,
        brief_realizer=realizer,
        semantic_reviewer=reviewer_factory(),
    )


def test_group_two_stage_accepts_realized_content() -> None:
    planner = planner_factory([[brief_payload("slot-1")]])
    realizer = realizer_factory([{
        "content": "今天先聊聊这个天气",
        "used_anchor_ids": ["f1"],
        "speech_act": "follow_up",
        "voice_profile_version": "style_contract_v3",
    }])
    engine = _sqlite_engine()

    with Session(engine) as session:
        results, tokens = generate_quality_results(
            session,
            _two_stage_request(),
            _two_stage_dependencies(planner, realizer),
        )

    assert results[0].rejection_code == ""
    assert str(results[0].content) == "今天先聊聊这个天气"
    assert results[0].quality_fallback == ""
    assert results[0].content.slot_id == "slot-1"
    assert tokens == 18


def test_group_two_stage_plan_rejection_preserves_slot_mapping() -> None:
    planner = planner_factory([[
        {"slot_id": "slot-1", "speech_act": "unsupported"},
    ]])

    def fail_realizer(*_args, **_kwargs):
        raise AssertionError("rejected plan must not realize")

    with Session(_sqlite_engine()) as session:
        results, _tokens = generate_quality_results(
            session,
            _two_stage_request(),
            _two_stage_dependencies(planner, fail_realizer),
        )

    assert results[0].rejection_code == "malformed_output"
    assert results[0].content.slot_id == "slot-1"


def test_group_two_stage_quality_wait_without_check_in_fallback() -> None:
    planner = planner_factory([[brief_payload("slot-1")]])
    output = {
        "content": "今天聊聊天气确实不错",
        "used_anchor_ids": ["f1"],
        "speech_act": "follow_up",
        "voice_profile_version": "style_contract_v3",
    }
    realizer = realizer_factory([output, output])
    engine = _sqlite_engine()

    with Session(engine) as session:
        results, _tokens = generate_quality_results(
            session,
            _two_stage_request(),
            _two_stage_dependencies(planner, realizer),
        )

    assert results[0].rejection_code == QUALITY_WAIT
    assert "template_shell_limited" in results[0].rejection_detail
    assert results[0].quality_fallback == ""
    assert str(results[0].content) != "签到"
    assert results[0].content.slot_id == "slot-1"
    assert len(realizer.calls) == 2
    assert "template_shell_limited" in realizer.calls[1]["user_prompt"]


def test_group_two_stage_silence_brief_never_calls_realizer() -> None:
    planner = planner_factory([[brief_payload("slot-1", speech_act="silence", anchor_ids=[])]])

    def fail_realizer(*_args, **_kwargs):
        raise AssertionError("silence brief must not realize")

    engine = _sqlite_engine()
    with Session(engine) as session:
        results, _tokens = generate_quality_results(
            session,
            _two_stage_request(),
            _two_stage_dependencies(planner, fail_realizer),
        )

    assert results[0].rejection_code == QUALITY_WAIT
    assert "brief_silence" in results[0].rejection_detail
    assert results[0].content.slot_id == "slot-1"


def test_group_two_stage_provider_unavailable_propagates_without_static_fallback() -> None:
    planner = planner_factory([[brief_payload("slot-1")]])
    realizer = realizer_factory([AiGenerationUnavailable("provider unavailable")])
    engine = _sqlite_engine()

    with Session(engine) as session:
        with pytest.raises(AiGenerationUnavailable, match="provider unavailable"):
            generate_quality_results(
                session,
                _two_stage_request(),
                _two_stage_dependencies(planner, realizer),
            )


def _question_briefs() -> list[dict]:
    return [
        brief_payload("slot-a", speech_act="question", length_band="micro", punctuation_profile="question"),
        brief_payload("slot-b", speech_act="question", length_band="short", punctuation_profile="question"),
        brief_payload("slot-c", speech_act="question", length_band="micro", punctuation_profile="question"),
        brief_payload("slot-d", speech_act="question", length_band="micro", punctuation_profile="question"),
    ]


def _question_shape(content: str) -> dict:
    return {
        "content": content,
        "used_anchor_ids": ["f1"],
        "speech_act": "question",
        "voice_profile_version": "style_contract_v3",
    }


def _structural_request():
    slots = [
        _quantity_slot("slot-a", 11),
        _quantity_slot("slot-b", 12),
        _quantity_slot("slot-c", 13),
        _quantity_slot("slot-d", 14),
    ]
    return _request(
        "",
        cached=False,
        config={"ai_two_stage_enabled": True, "generation_slots": slots},
        batch_ids=["action-1", "action-2", "action-3", "action-4"],
        quality_snapshots=[{"account_profile": "", "stance_summary": ""}] * 4,
    )


def test_group_two_stage_structural_duplicate_falls_to_quality_wait() -> None:
    planner = planner_factory([_question_briefs()])
    realizer = realizer_factory([
        _question_shape("今天聊不聊呢？"),
        _question_shape("今天到底还聊不聊呢？"),
        _question_shape("今天还聊不聊呢？"),
        _question_shape("今天想聊不聊呢？"),
        _question_shape("今天再聊不聊呢？"),
        _question_shape("今天又聊不聊呢？"),
    ])

    with Session(_sqlite_engine()) as session:
        results, _tokens = generate_quality_results(
            session,
            _structural_request(),
            _two_stage_dependencies(planner, realizer),
        )

    assert [result.rejection_code for result in results] == [
        "",
        "",
        QUALITY_WAIT,
        QUALITY_WAIT,
    ]
    assert len(realizer.calls) == 6


def _comment_request(
    *,
    flag: bool = True,
    grounding: bool = False,
    reply: bool = False,
):
    return SimpleNamespace(
        tenant_id=1,
        config={
            "ai_two_stage_enabled": flag,
            "channel_comment_grounding_v1_enabled": grounding,
        },
        payload=SimpleNamespace(
            slot_id="comment-slot-1",
            message_content="频道正文里提到周五上新",
            reply_to_message_id="8101" if reply else "",
            reply_target_preview="这个尺寸是多少" if reply else "",
            grounding_snapshot_id="",
            grounding_assignment_id="",
            grounding_enrollment_id="",
            grounding_teacher_candidate_id="",
            grounding_primary_evidence_id="",
            grounding_speech_act="",
        ),
        account_id=11,
    )


def test_comment_two_stage_blocked_quality_wait_when_quality_rejects(monkeypatch) -> None:
    planner = planner_factory([[brief_payload("comment-slot-1")]])
    output = {
        "content": "周五这次上新感觉还不错",
        "used_anchor_ids": ["f1"],
        "speech_act": "follow_up",
        "voice_profile_version": "style_contract_v3",
    }
    realizer = realizer_factory([output, output])

    def reject_evaluate(_session, _request, content, *, action_loader, **_kwargs):
        return SimpleNamespace(
            allowed=False,
            code="duplicate_rejected",
            detail="与历史评论相似",
            content=content,
            audit={},
        )

    monkeypatch.setattr(comment_generation_pipeline, "_evaluate_candidate", reject_evaluate)
    with Session(_sqlite_engine()) as session:
        with pytest.raises(CommentGenerationBlocked) as exc_info:
            comment_generation_pipeline._run_two_stage_comment(
                session,
                _comment_request(),
                comment_generation_pipeline.CommentGenerationDependencies(
                    brief_planner=planner,
                    brief_realizer=realizer,
                    semantic_reviewer=reviewer_factory(),
                ),
                action_loader=lambda *_args: None,
            )

    assert exc_info.value.code == QUALITY_WAIT
    assert len(realizer.calls) == 2
    assert "duplicate_rejected" in realizer.calls[1]["user_prompt"]


def test_comment_two_stage_never_falls_back_to_emoji(monkeypatch) -> None:
    planner = planner_factory([[{"slot_id": "comment-slot-1", "speech_act": "shout"}]])

    def fail_realizer(*_args, **_kwargs):
        raise AssertionError("brief rejected; realizer must not run")

    monkeypatch.setattr(
        comment_generation_pipeline,
        "_emoji_fallback_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("emoji fallback forbidden")),
    )
    with Session(_sqlite_engine()) as session:
        with pytest.raises(CommentGenerationBlocked) as exc_info:
            comment_generation_pipeline._run_two_stage_comment(
                session,
                _comment_request(),
                comment_generation_pipeline.CommentGenerationDependencies(
                    brief_planner=planner,
                    brief_realizer=fail_realizer,
                    semantic_reviewer=reviewer_factory(),
                ),
                action_loader=lambda *_args: None,
            )

    assert exc_info.value.code == QUALITY_WAIT
    assert "brief_schema_invalid" in exc_info.value.detail


def test_grounding_reply_quality_exhaustion_becomes_reply_shortfall(monkeypatch) -> None:
    planner = planner_factory([[
        brief_payload("comment-slot-1", reply_to_message_id="8101"),
    ]])
    output = {
        "content": "这条回复没有回答读者问题",
        "used_anchor_ids": ["f1"],
        "speech_act": "follow_up",
        "voice_profile_version": "style_contract_v3",
    }
    realizer = realizer_factory([output, output])

    def reject_evaluate(_session, _request, content, *, action_loader, **_kwargs):
        return SimpleNamespace(
            allowed=False,
            code="reply_relation_rejected",
            detail="没有回答引用目标",
            content=content,
            audit={},
        )

    monkeypatch.setattr(comment_generation_pipeline, "_evaluate_candidate", reject_evaluate)
    monkeypatch.setattr(
        comment_generation_pipeline,
        "_emoji_fallback_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reply slot must not enter fallback selection")
        ),
    )
    with Session(_sqlite_engine()) as session:
        with pytest.raises(CommentGenerationBlocked) as exc_info:
            comment_generation_pipeline._run_two_stage_comment(
                session,
                _comment_request(grounding=True, reply=True),
                comment_generation_pipeline.CommentGenerationDependencies(
                    brief_planner=planner,
                    brief_realizer=realizer,
                    semantic_reviewer=reviewer_factory(),
                ),
                action_loader=lambda *_args: None,
            )

    assert exc_info.value.code == "reply_quality_shortfall"
    assert exc_info.value.tokens > 0
