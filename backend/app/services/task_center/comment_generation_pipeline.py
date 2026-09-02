from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from app.models import Action, AiAccountVoiceProfile
from app.services.antigravity_provider_client import AntigravityProviderResultUnknown

from .account_voice_profile_cache import (
    VOICE_PROFILE_CONTRACT_VERSION,
    voice_profile_snapshot_hash,
)
from .ai_generation_state import GenerationAttemptStale
from .ai_generator import (
    clean_channel_comment_contents,
    generate_channel_comments,
    generate_channel_reply_comments,
)
from .channel_payloads import PostCommentPayload
from .comment_generation_quality import (
    evaluate_comment_fallback_quality,
    evaluate_comment_generation_quality,
)
from .comment_two_stage_generation import (
    CommentGenerationBlocked,
    TwoStageCommentHooks,
    generate_two_stage_comment,
)
from .two_stage_generation import QUALITY_WAIT, two_stage_enabled
from .comment_fallback_selection import (
    CommentFallbackUnavailable,
    select_comment_fallback,
)
from .comment_generation_helpers import (
    COMMENT_EMOJI_FALLBACKS,
    STRUCTURAL_COMMENT_FAILURES,
    UNICODE_EMOJI_ALLOWLIST_V2,
    comment_generation_stages as _comment_generation_stages,
    fallback_reason as _fallback_reason,
    grounding_contract as _grounding_contract,
    mask_fallback_attempt as _mask_fallback_attempt,
    ordered_fallback_emojis as _ordered_fallback_emojis,
    provider_failure as _provider_failure,
    quality_attempt as _quality_attempt,
    reply_target as _reply_target,
)


REPLY_QUALITY_SHORTFALL = "reply_quality_shortfall"


def _commit_session(session: Session) -> None:
    session.commit()


@dataclass(frozen=True)
class CommentGenerationDependencies:
    direct_generator: Callable = generate_channel_comments
    reply_generator: Callable = generate_channel_reply_comments
    phase_c_commit: Callable[[Session], None] = _commit_session
    # 两阶段生成（PRD §5.4）注入点；None 时使用 two_stage_generation 默认通道。
    brief_planner: Callable | None = None
    brief_realizer: Callable | None = None
    semantic_reviewer: Callable | None = None


@dataclass(frozen=True)
class CommentGenerationRequest:
    action_id: str
    tenant_id: int
    task_id: str
    account_id: int
    payload: PostCommentPayload
    config: dict
    attempt_id: str
    request_id: str
    claim_owner: str
    claim_token: str
    cached_content: str
    cached_tokens: int
    cached_fallback_kind: str
    cached_fallback_reason: str
    cached_attempts: tuple[dict, ...]
    cached_media_segment: dict | None = None
    cached_selection_metadata: dict | None = None

    @property
    def has_cached_result(self) -> bool:
        return bool(self.cached_content or self.cached_media_segment)


@dataclass(frozen=True)
class GeneratedCommentResult:
    content: str
    tokens: int
    fallback_kind: str = ""
    fallback_reason: str = ""
    attempts: tuple[dict, ...] = ()
    quality_audit: dict | None = None
    media_segment: dict | None = None
    selection_metadata: dict | None = None


def generate_comment_result(
    session: Session,
    request: CommentGenerationRequest,
    dependencies: CommentGenerationDependencies,
    *,
    action_loader: Callable[[Session, CommentGenerationRequest], Action],
) -> GeneratedCommentResult:
    session.rollback()
    if request.has_cached_result:
        return _cached_result(request)
    planned_fallback = request.payload.comment_fallback_intent_kind == "planned"
    if planned_fallback and not request.payload.grounding_assignment_id:
        return _emoji_fallback_result(
            session,
            request,
            total_tokens=0,
            attempts=[{"stage": "planned_fallback", "outcome": "selected"}],
            action_loader=action_loader,
        )
    mask_reason = _comment_mask_fallback_reason(session, request)
    session.rollback()
    two_stage = two_stage_enabled(request.config)
    if mask_reason:
        if two_stage:
            raise CommentGenerationBlocked(
                _quality_wait_code(request),
                f"mask_not_ready:{mask_reason}",
            )
        return _emoji_fallback_result(
            session,
            request,
            total_tokens=0,
            attempts=[_mask_fallback_attempt(mask_reason)],
            action_loader=action_loader,
        )
    if session.in_transaction():
        raise RuntimeError("comment generation transaction boundary is open")
    if two_stage:
        return _run_two_stage_comment(
            session,
            request,
            dependencies,
            action_loader=action_loader,
        )
    return _run_generation_stages(
        session,
        request,
        dependencies,
        action_loader=action_loader,
    )


def _run_generation_stages(
    session: Session,
    request: CommentGenerationRequest,
    dependencies: CommentGenerationDependencies,
    *,
    action_loader: Callable,
) -> GeneratedCommentResult:
    attempts: list[dict] = []
    total_tokens = 0
    for stage in _comment_generation_stages():
        try:
            contents, tokens = _call_generator(
                session, request, dependencies, stage=stage,
            )
        except GenerationAttemptStale:
            raise
        except AntigravityProviderResultUnknown:
            raise
        except Exception as exc:
            _close_failed_stage_transaction(session)
            attempts.append(_provider_failure(stage, exc))
            continue
        total_tokens += int(tokens or 0)
        cleaned = clean_channel_comment_contents(list(contents or []), limit=1)
        if not cleaned:
            attempts.append({"stage": stage, "outcome": "candidate_missing"})
            continue
        decision = _evaluate_candidate(
            session,
            request,
            str(cleaned[0]).strip(),
            action_loader=action_loader,
        )
        attempts.append(_quality_attempt(stage, decision))
        if decision.allowed:
            return GeneratedCommentResult(
                decision.content,
                total_tokens,
                attempts=tuple(attempts),
                quality_audit=decision.audit,
            )
        if decision.code in STRUCTURAL_COMMENT_FAILURES:
            raise CommentGenerationBlocked(decision.code, decision.detail)
    return _emoji_fallback_result(
        session,
        request,
        total_tokens=total_tokens,
        attempts=attempts,
        action_loader=action_loader,
    )


def _close_failed_stage_transaction(session: Session) -> None:
    if session.in_transaction():
        session.rollback()


def _run_two_stage_comment(
    session: Session,
    request: CommentGenerationRequest,
    dependencies: CommentGenerationDependencies,
    *,
    action_loader: Callable,
) -> GeneratedCommentResult:
    """评论两阶段生成（PRD §5.4）：Brief → Realizer → 既有质量闸。

    Stage 2 失败按 rejection code 定向重生成一次；耗尽进入 quality_wait，
    不发送 Stage 1 草稿，也不降级 emoji。
    """
    hooks = TwoStageCommentHooks(
        brief_planner=dependencies.brief_planner,
        brief_realizer=dependencies.brief_realizer,
        semantic_reviewer=dependencies.semantic_reviewer,
        evaluate_candidate=_evaluate_candidate,
        action_loader=action_loader,
        structural_failure_codes=STRUCTURAL_COMMENT_FAILURES,
    )
    try:
        result = generate_two_stage_comment(session, request, hooks)
    except CommentGenerationBlocked as exc:
        if exc.code == QUALITY_WAIT and _grounding_reply(request):
            raise CommentGenerationBlocked(
                REPLY_QUALITY_SHORTFALL,
                exc.detail,
                evaluator_evidence=exc.evaluator_evidence,
                tokens=exc.tokens,
            ) from exc
        if (
            exc.code != QUALITY_WAIT
            or not _grounding_contract(request)
        ):
            raise
        return _emoji_fallback_result(
            session,
            request,
            total_tokens=exc.tokens,
            attempts=[{"stage": "two_stage", "outcome": "quality_exhausted", "reason": exc.detail}],
            action_loader=action_loader,
        )
    return GeneratedCommentResult(
        result.content, result.tokens,
        attempts=result.attempts, quality_audit=result.quality_audit,
    )


def _comment_mask_fallback_reason(
    session: Session,
    request: CommentGenerationRequest,
) -> str:
    payload = request.payload
    if payload.deterministic_fallback_reason:
        return payload.deterministic_fallback_reason
    if payload.mask_status != "active":
        return "mask_missing"
    mask = session.get(AiAccountVoiceProfile, payload.account_mask_id)
    valid = bool(
        mask
        and mask.tenant_id == request.tenant_id
        and mask.account_id == request.account_id
        and mask.version == payload.account_mask_version
        and mask.status == "active"
        and mask.quality_status == "active"
        and mask.short_prompt_summary
        and payload.voice_profile_contract_version == VOICE_PROFILE_CONTRACT_VERSION
        and payload.account_mask_snapshot_hash == voice_profile_snapshot_hash(mask)
    )
    return "" if valid else "mask_missing"


def _call_generator(
    session: Session,
    request: CommentGenerationRequest,
    dependencies: CommentGenerationDependencies,
    *,
    stage: str,
) -> tuple[list[str], int]:
    config = dict(request.config)
    if stage.startswith("fallback_"):
        config["_ai_fallback_stage"] = stage
    payload = request.payload
    config["_comment_slot_ordinal"] = max(
        0, int(getattr(payload, "target_ordinal", 0) or 0) - 1,
    )
    if _grounding_contract(request):
        config["channel_comment_grounding_v1_enabled"] = True
        config["_comment_grounding_assignment"] = _grounding_assignment_payload(payload)
    if payload.reply_to_message_id:
        return dependencies.reply_generator(
            session,
            request.tenant_id,
            config,
            reply_targets=[_reply_target(payload)],
            message_content=payload.message_content,
            target_label=payload.target_display,
        )
    return dependencies.direct_generator(
        session,
        request.tenant_id,
        config,
        count=1,
        message_content=payload.message_content,
        target_label=payload.target_display,
    )


def _grounding_assignment_payload(payload: object) -> dict:
    required = {
        "snapshot_id": str(getattr(payload, "grounding_snapshot_id", "") or ""),
        "assignment_id": str(getattr(payload, "grounding_assignment_id", "") or ""),
        "relation_kind": "reply" if payload.reply_to_message_id else "direct",
        "primary_evidence_id": str(
            getattr(payload, "grounding_primary_evidence_id", "") or "",
        ),
        "secondary_evidence_id": str(
            getattr(payload, "grounding_secondary_evidence_id", "") or "",
        ),
        "primary_aspect_code": str(
            getattr(payload, "grounding_primary_aspect_code", "") or "",
        ),
        "primary_aspect_text": str(
            getattr(payload, "grounding_primary_aspect_text", "") or "",
        ),
        "teacher_candidate_id": str(
            getattr(payload, "grounding_teacher_candidate_id", "") or "",
        ),
        "teacher_name": str(getattr(payload, "grounding_teacher_name", "") or ""),
        "speech_act": str(getattr(payload, "grounding_speech_act", "") or ""),
    }
    if not all(required[key] for key in (
        "snapshot_id", "assignment_id", "primary_evidence_id",
        "primary_aspect_code", "primary_aspect_text", "speech_act",
    )):
        raise AiGenerationUnavailable("channel_comment_grounding_assignment_incomplete")
    return required


def _evaluate_candidate(
    session: Session,
    request: CommentGenerationRequest,
    content: str,
    *,
    action_loader: Callable,
    fallback: bool = False,
):
    action = action_loader(session, request)
    evaluator = (
        evaluate_comment_fallback_quality
        if fallback
        else evaluate_comment_generation_quality
    )
    decision = evaluator(
        session,
        action,
        payload=request.payload,
        content=content,
    )
    session.rollback()
    return decision


def _emoji_fallback_result(
    session: Session,
    request: CommentGenerationRequest,
    *,
    total_tokens: int,
    attempts: list[dict],
    action_loader: Callable,
) -> GeneratedCommentResult:
    if _grounding_reply(request):
        raise CommentGenerationBlocked(
            REPLY_QUALITY_SHORTFALL,
            "reply slot quality exhausted; fallback content is forbidden",
            tokens=total_tokens,
        )
    fallback_reason = _fallback_reason(attempts)
    if _grounding_contract(request):
        return _v2_fallback_result(
            session,
            request,
            total_tokens=total_tokens,
            attempts=attempts,
            fallback_reason=fallback_reason,
            action_loader=action_loader,
        )
    for emoji in _ordered_fallback_emojis(request):
        decision = _evaluate_candidate(
            session,
            request,
            emoji,
            fallback=True,
            action_loader=action_loader,
        )
        if decision.allowed:
            return GeneratedCommentResult(
                decision.content,
                total_tokens,
                fallback_kind="emoji_text",
                fallback_reason=fallback_reason,
                attempts=tuple(attempts),
                quality_audit=decision.audit,
            )
        if decision.code in STRUCTURAL_COMMENT_FAILURES:
            raise CommentGenerationBlocked(decision.code, decision.detail)
    raise CommentGenerationBlocked(
        "fallback_outbound_policy_blocked",
        "审核白名单评论表情均被出站安全策略拒绝",
    )


def _v2_fallback_result(
    session: Session,
    request: CommentGenerationRequest,
    *,
    total_tokens: int,
    attempts: list[dict],
    fallback_reason: str,
    action_loader: Callable,
) -> GeneratedCommentResult:
    try:
        selected = select_comment_fallback(
            session,
            action_id=request.action_id,
            tenant_id=request.tenant_id,
            task_id=request.task_id,
            content_mix_contract_id=request.payload.content_mix_contract_id,
            target_ordinal=int(request.payload.target_ordinal or 0),
            fallback_reason=fallback_reason,
            fallback_kind=request.payload.comment_fallback_intent_kind,
        )
    except CommentFallbackUnavailable as exc:
        raise CommentGenerationBlocked(exc.code, str(exc)) from exc
    if selected.content_kind == "image_meme":
        return GeneratedCommentResult(
            "", total_tokens, "image_meme", fallback_reason,
            tuple(attempts), {"fallback_selection": selected.metadata},
            selected.media_segment, selected.metadata,
        )
    decision = _evaluate_candidate(
        session, request, selected.content, fallback=True,
        action_loader=action_loader,
    )
    if not decision.allowed:
        raise CommentGenerationBlocked(decision.code, decision.detail)
    audit = {**(decision.audit or {}), "fallback_selection": selected.metadata}
    return GeneratedCommentResult(
        decision.content, total_tokens, "unicode_emoji", fallback_reason,
        tuple(attempts), audit, None, selected.metadata,
    )


def _grounding_reply(request: CommentGenerationRequest) -> bool:
    return bool(_grounding_contract(request) and request.payload.reply_to_message_id)


def _quality_wait_code(request: CommentGenerationRequest) -> str:
    return REPLY_QUALITY_SHORTFALL if _grounding_reply(request) else QUALITY_WAIT


def _cached_result(request: CommentGenerationRequest) -> GeneratedCommentResult:
    return GeneratedCommentResult(
        request.cached_content,
        request.cached_tokens,
        request.cached_fallback_kind,
        request.cached_fallback_reason,
        request.cached_attempts,
        media_segment=request.cached_media_segment,
        selection_metadata=request.cached_selection_metadata,
    )


__all__ = [
    "CommentGenerationBlocked",
    "CommentGenerationDependencies",
    "CommentGenerationRequest",
    "GeneratedCommentResult",
    "generate_comment_result",
]
