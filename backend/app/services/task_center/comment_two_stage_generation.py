from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .ai_generator import clean_channel_comment_contents
from .two_stage_generation import (
    QUALITY_WAIT,
    TWO_STAGE_REALIZE_ATTEMPTS,
    TwoStageRealizeError,
    comment_history_lines,
    plan_message_briefs,
    realize_message_content,
)


@dataclass(frozen=True)
class TwoStageCommentHooks:
    brief_planner: Callable | None
    brief_realizer: Callable | None
    semantic_reviewer: Callable | None
    evaluate_candidate: Callable
    action_loader: Callable
    structural_failure_codes: frozenset[str]


@dataclass(frozen=True)
class TwoStageCommentResult:
    content: str
    tokens: int
    attempts: tuple[dict, ...]
    quality_audit: dict | None


@dataclass(frozen=True)
class _Attempt:
    content: str = ""
    quality_audit: dict | None = None
    event: dict | None = None
    feedback: str = ""
    tokens: int = 0
    evaluator_evidence: dict | None = None


class CommentGenerationBlocked(RuntimeError):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        evaluator_evidence: dict | None = None,
        tokens: int = 0,
    ):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.evaluator_evidence = dict(evaluator_evidence or {})
        self.tokens = max(0, int(tokens or 0))


def generate_two_stage_comment(
    session: Session,
    request,
    hooks: TwoStageCommentHooks,
) -> TwoStageCommentResult:
    history_lines = comment_history_lines(request.payload)
    plan, total_tokens = _plan_comment(
        session, request, hooks, history_lines=history_lines,
    )
    attempts: list[dict] = []
    feedback = ""
    last_evidence: dict = {}
    for _attempt_no in range(TWO_STAGE_REALIZE_ATTEMPTS):
        result = _run_attempt(
            session, request, hooks, plan=plan,
            history_lines=history_lines, feedback=feedback,
        )
        total_tokens += result.tokens
        attempts.append(dict(result.event or {}))
        last_evidence = dict(result.evaluator_evidence or last_evidence)
        if result.content:
            return TwoStageCommentResult(
                result.content, total_tokens, tuple(attempts), result.quality_audit,
            )
        feedback = result.feedback
    raise CommentGenerationBlocked(
        QUALITY_WAIT,
        "两阶段评论生成质量预算耗尽，进入 quality_wait",
        evaluator_evidence=last_evidence,
        tokens=total_tokens,
    )


def _plan_comment(session: Session, request, hooks: TwoStageCommentHooks, *, history_lines: list[str]):
    plans, tokens = plan_message_briefs(
        session,
        request.tenant_id,
        request.config,
        history_lines=history_lines,
        slots=[_comment_slot(request)],
        planner=hooks.brief_planner,
    )
    plan = plans[0] if plans else None
    invalid = plan is None or plan.rejection_code or plan.brief is None
    silence = not invalid and plan.brief.speech_act == "silence"
    if invalid or silence:
        code = plan.rejection_code if plan is not None and plan.rejection_code else "brief_silence"
        detail = plan.rejection_detail if plan is not None and plan.rejection_detail else "上下文不支持安全评论"
        raise CommentGenerationBlocked(QUALITY_WAIT, f"{code}:{detail}")
    return plan, tokens


def _comment_slot(request) -> dict:
    return {
        "slot_id": str(request.payload.slot_id or ""),
        "account_id": int(request.account_id or 0),
        "reply_to_message_id": str(request.payload.reply_to_message_id or ""),
        "reply_preview": str(request.payload.reply_target_preview or ""),
        **dict(request.config.get("_ai_content_contract") or {}),
    }


def _run_attempt(
    session: Session,
    request,
    hooks: TwoStageCommentHooks,
    *,
    plan,
    history_lines: list[str],
    feedback: str,
) -> _Attempt:
    try:
        content, meta, spent = realize_message_content(
            session, request.tenant_id, request.config, plan,
            history_lines=history_lines, rejection_feedback=feedback,
            realizer=hooks.brief_realizer, reviewer=hooks.semantic_reviewer,
        )
    except TwoStageRealizeError as exc:
        event = {"stage": "two_stage_realize", "outcome": "rejected", "reason": exc.code}
        return _Attempt(
            event=event, feedback=exc.code, tokens=exc.tokens,
            evaluator_evidence=exc.evidence,
        )
    cleaned = clean_channel_comment_contents([content], limit=1)
    if not cleaned:
        event = {"stage": "two_stage_realize", "outcome": "candidate_missing"}
        return _Attempt(event=event, feedback="candidate_missing", tokens=spent)
    return _evaluate_attempt(
        session, request, hooks, str(cleaned[0]).strip(), meta=meta, spent=spent,
    )


def _evaluate_attempt(
    session: Session,
    request,
    hooks: TwoStageCommentHooks,
    content: str,
    *,
    meta: dict,
    spent: int,
) -> _Attempt:
    decision = hooks.evaluate_candidate(
        session, request, content, action_loader=hooks.action_loader,
    )
    event = {
        "stage": "two_stage_realize",
        "outcome": "accepted" if decision.allowed else "rejected",
        "reason": decision.code,
    }
    if decision.code in hooks.structural_failure_codes:
        raise CommentGenerationBlocked(decision.code, decision.detail)
    if not decision.allowed:
        return _Attempt(
            event=event, feedback=f"{decision.code}:{decision.detail}",
            tokens=spent, evaluator_evidence=meta,
        )
    audit = {**(decision.audit or {}), "two_stage_evaluator_evidence": meta}
    return _Attempt(
        content=decision.content, quality_audit=audit, event=event,
        tokens=spent, evaluator_evidence=meta,
    )


__all__ = [
    "CommentGenerationBlocked",
    "TwoStageCommentHooks",
    "TwoStageCommentResult",
    "generate_two_stage_comment",
]
