from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from app.models import Action, AiAccountVoiceProfile

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
    UNICODE_EMOJI_ALLOWLIST_V2,
    select_comment_fallback,
)


COMMENT_GENERATION_ATTEMPTS_PER_MODEL = 3
COMMENT_EMOJI_FALLBACKS = ("👍", "🙂", "👏")
STRUCTURAL_COMMENT_FAILURES = frozenset(
    {
        "comment_unavailable_message",
        "peer_invalid",
        "reply_target_missing",
        "reply_target_stale",
        "rule_version_unavailable",
    }
)


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
    if (
        request.payload.comment_fallback_intent_kind == "planned"
        and not request.payload.grounding_assignment_id
    ):
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
            # 两阶段合同（PRD §5.4）：面具不就绪不降级 emoji，进入 quality_wait。
            raise CommentGenerationBlocked(
                QUALITY_WAIT,
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
        if (
            exc.code != QUALITY_WAIT
            or not request.config.get("channel_comment_grounding_v1_enabled")
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
    fallback_reason = _fallback_reason(attempts)
    if request.config.get("channel_comment_grounding_v1_enabled"):
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


def _comment_generation_stages() -> tuple[str, ...]:
    return (
        *("primary_m3" for _ in range(COMMENT_GENERATION_ATTEMPTS_PER_MODEL)),
        *("fallback_m25" for _ in range(COMMENT_GENERATION_ATTEMPTS_PER_MODEL)),
    )


def _ordered_fallback_emojis(
    request: CommentGenerationRequest,
) -> tuple[str, ...]:
    key = f"{request.task_id}:{request.payload.channel_message_id}:{request.payload.slot_id}"
    offset = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)
    pool = (
        UNICODE_EMOJI_ALLOWLIST_V2
        if bool(
            getattr(request, "config", {}).get("channel_comment_grounding_v1_enabled")
            or getattr(request, "config", {}).get("unicode_emoji_allowlist_v2")
        )
        else COMMENT_EMOJI_FALLBACKS
    )
    index = offset % len(pool)
    return pool[index:] + pool[:index]


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


def _mask_fallback_attempt(reason: str) -> dict:
    return {
        "stage": "phase_a",
        "outcome": "deterministic_fallback",
        "reason": reason,
    }


def _provider_failure(stage: str, exc: Exception) -> dict:
    return {
        "stage": stage,
        "outcome": "provider_failed",
        "reason": str(exc),
    }


def _quality_attempt(stage: str, decision) -> dict:
    return {
        "stage": stage,
        "outcome": "accepted" if decision.allowed else "rejected",
        "reason": decision.code,
    }


def _fallback_reason(attempts: list[dict]) -> str:
    reasons = [
        str(item.get("reason") or item.get("outcome") or "")
        for item in attempts
    ]
    return ",".join(item for item in reasons if item) or "all_model_stages_rejected"


def _reply_target(payload: PostCommentPayload) -> dict:
    return {
        "message_id": int(payload.reply_to_message_id or 0),
        "author": payload.reply_target_author,
        "preview": payload.reply_target_preview,
        "source": payload.reply_target_source,
    }


__all__ = [
    "CommentGenerationBlocked",
    "CommentGenerationDependencies",
    "CommentGenerationRequest",
    "GeneratedCommentResult",
    "generate_comment_result",
]
