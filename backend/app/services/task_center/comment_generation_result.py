from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Action

from .channel_payloads import PostCommentPayload
from .comment_generation_pipeline import GeneratedCommentResult
from .comment_generation_quality import (
    CommentQualityDecision,
    evaluate_comment_generation_quality,
)


def evaluate_legacy_generated_comment(
    session: Session,
    action: Action,
    *,
    payload: PostCommentPayload,
    content: str,
    tokens: int,
) -> GeneratedCommentResult:
    decision = evaluate_comment_generation_quality(
        session,
        action,
        payload=payload,
        content=content,
    )
    if decision.allowed:
        return GeneratedCommentResult(
            decision.content,
            tokens,
            quality_audit=decision.audit,
        )
    return GeneratedCommentResult(
        "",
        tokens,
        fallback_reason=f"{decision.code}:{decision.detail}",
        quality_audit={
            **(decision.audit or {}),
            "rejection_code": decision.code,
            "rejection_detail": decision.detail,
        },
    )


def generated_comment_decision(
    result: GeneratedCommentResult,
) -> CommentQualityDecision:
    audit = result.quality_audit or {}
    rejection_code = str(audit.get("rejection_code") or "")
    if rejection_code:
        return CommentQualityDecision(
            False,
            "",
            rejection_code,
            str(audit.get("rejection_detail") or ""),
            audit,
        )
    return CommentQualityDecision(
        True,
        result.content,
        audit=audit,
    )


__all__ = [
    "evaluate_legacy_generated_comment",
    "generated_comment_decision",
]
