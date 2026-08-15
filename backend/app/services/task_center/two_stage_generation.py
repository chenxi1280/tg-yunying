from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai_gateway import canonical_ai_model_identity
from app.models import AiAccountVoiceProfile

from .ai_generator import (
    TWO_STAGE_BRIEF_PURPOSE,
    TWO_STAGE_REALIZE_PURPOSE,
    TWO_STAGE_REVIEW_PURPOSE,
    generate_structured_payloads,
)
from .message_brief import (
    BRIEF_PLANNER_SYSTEM_PROMPT,
    VOICE_REALIZER_SYSTEM_PROMPT,
    MessageBrief,
    batch_style_collapse_reason,
    build_brief_planner_user_prompt,
    build_realizer_user_prompt,
    fact_id_map,
    parse_brief_item,
    parse_realizer_response,
    voice_contract_v3,
)
from .semantic_grounding import lexical_grounding_evidence


TWO_STAGE_FLAG = "ai_two_stage_enabled"
TWO_STAGE_REALIZE_ATTEMPTS = 2
QUALITY_WAIT = "quality_wait"
SEMANTIC_REVIEW_PROMPT_VERSION = "semantic_reviewer_v1"
SEMANTIC_REVIEW_CONFIDENCE_THRESHOLD = 0.8
SEMANTIC_REVIEW_SYSTEM_PROMPT = """你是独立内容审核模型，不改写文案。
先逐项核对事实支持、上下文承接和账号声线，再给结论；不得因文案更长或位置靠前而偏好。
只依据输入的 allowed_facts、brief、voice_profile 与 candidate，不使用外部知识。
输出 JSON：decision(pass/fail/uncertain)、confidence(0-1)、codes、evidence、prompt_version。"""

BriefPlanner = Callable[..., tuple[object, int]]
BriefRealizer = Callable[..., tuple[object, int]]
SemanticReviewer = Callable[..., tuple[object, int]]


def two_stage_enabled(config: dict | None) -> bool:
    """任务级两阶段生成开关（PRD §7.2.6：按任务 flag 灰度，不热改全租户）。"""
    return bool((config or {}).get(TWO_STAGE_FLAG))


class TwoStageRealizeError(RuntimeError):
    def __init__(self, code: str, *, evidence: dict | None = None, tokens: int = 0):
        super().__init__(code)
        self.code = code
        self.evidence = dict(evidence or {})
        self.tokens = max(0, int(tokens or 0))


@dataclass(frozen=True)
class TwoStagePlan:
    slot_id: str
    account_id: int = 0
    brief: MessageBrief | None = None
    rejection_code: str = ""
    rejection_detail: str = ""
    reply_preview: str = ""


def _default_planner(session, tenant_id, config, *, system_prompt, user_prompt, count):
    return generate_structured_payloads(
        session,
        tenant_id,
        config,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        purpose=TWO_STAGE_BRIEF_PURPOSE,
        count=count,
    )


def _default_realizer(session, tenant_id, config, *, system_prompt, user_prompt, count=1):
    return generate_structured_payloads(
        session,
        tenant_id,
        config,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        purpose=TWO_STAGE_REALIZE_PURPOSE,
        count=1,
    )


def _default_reviewer(session, tenant_id, config, *, system_prompt, user_prompt, count=1):
    reviewer_model = _validate_default_reviewer_config(config)
    reviewer_config = {**config, "ai_model": reviewer_model}
    return generate_structured_payloads(
        session,
        tenant_id,
        reviewer_config,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        purpose=TWO_STAGE_REVIEW_PURPOSE,
        count=1,
    )


def _validate_default_reviewer_config(config: dict) -> str:
    reviewer_model = str(config.get("ai_semantic_reviewer_model") or "").strip()
    generator_model = str(config.get("ai_model") or "").strip()
    if not reviewer_model:
        raise TwoStageRealizeError("semantic_reviewer_model_missing")
    if not generator_model:
        raise TwoStageRealizeError("semantic_generator_model_missing")
    if _model_identity(reviewer_model) == _model_identity(generator_model):
        raise TwoStageRealizeError("semantic_reviewer_must_differ_from_generator")
    return reviewer_model


def _model_identity(model_name: str) -> str:
    return canonical_ai_model_identity(model_name)


def _brief_items(payload: object) -> list[object]:
    if isinstance(payload, dict):
        for key in ("briefs", "items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return [payload]
    if isinstance(payload, list):
        return payload
    return []


def plan_message_briefs(
    session: Session,
    tenant_id: int,
    config: dict,
    *,
    history_lines: list[str],
    slots: list[dict],
    planner: BriefPlanner | None = None,
) -> tuple[list[TwoStagePlan], int]:
    """为每个 slot 冻结 brief；批次塌缩时只重规划一次。"""
    planner = planner or _default_planner
    facts = fact_id_map(history_lines)
    fact_entries = [{"fact_id": fact_id, "text": text} for fact_id, text in facts.items()]
    slot_infos = _brief_slot_infos(slots)
    total_tokens = 0
    recent: list[dict] = []
    plans: list[TwoStagePlan] = []
    feedback = ""
    for attempt in range(2):
        user_prompt = build_brief_planner_user_prompt(
            slot_infos=slot_infos,
            allowed_facts=fact_entries,
            recent_briefs=recent,
        )
        # planner 回调约定：可注入 (session, tenant_id, config, *, system_prompt, user_prompt, count)
        payload, tokens = planner(
            session,
            tenant_id,
            config,
            system_prompt=BRIEF_PLANNER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            count=len(slot_infos),
        )
        total_tokens += int(tokens or 0)
        plans = _parse_plans(payload, slot_infos=slot_infos, fact_ids=tuple(facts.keys()))
        briefs = [plan.brief for plan in plans if plan.brief is not None]
        feedback = batch_style_collapse_reason(briefs)
        if not feedback:
            return plans, total_tokens
        recent = [
            {
                "slot_id": brief.slot_id,
                "speech_act": brief.speech_act,
                "length_band": brief.length_band,
                "punctuation_profile": brief.punctuation_profile,
            }
            for brief in briefs
        ]
    # 重规划后仍塌缩：塌缩组合里的 slot 全部 typed 拒绝，不再静默照发。
    return _mark_collapsed(plans), total_tokens


def _brief_slot_infos(slots: list[dict]) -> list[dict]:
    return [
        {
            "slot_id": str(slot.get("slot_id") or ""),
            "account_id": int(slot.get("account_id") or 0),
            "reply_to_message_id": str(slot.get("reply_to_message_id") or ""),
            "reply_preview": str(slot.get("reply_preview") or "")[:120],
        }
        for slot in slots
    ]


def _parse_plans(
    payload: object,
    *,
    slot_infos: list[dict],
    fact_ids: tuple[str, ...],
) -> list[TwoStagePlan]:
    items = _brief_items(payload)
    plans: list[TwoStagePlan] = []
    for index, info in enumerate(slot_infos):
        item = items[index] if index < len(items) else None
        brief = parse_brief_item(item, slot_id=str(info["slot_id"]), valid_fact_ids=fact_ids)
        if brief is None:
            plans.append(TwoStagePlan(
                slot_id=str(info["slot_id"]),
                account_id=int(info["account_id"]),
                rejection_code="brief_schema_invalid",
                rejection_detail="brief 枚举值或锚点引用非法",
                reply_preview=str(info.get("reply_preview") or ""),
            ))
            continue
        if brief.reply_to_message_id != str(info.get("reply_to_message_id") or ""):
            plans.append(TwoStagePlan(
                slot_id=str(info["slot_id"]),
                account_id=int(info["account_id"]),
                rejection_code="brief_reply_target_mismatch",
                rejection_detail="brief reply_to_message_id 与 slot 不一致",
                reply_preview=str(info.get("reply_preview") or ""),
            ))
            continue
        plans.append(TwoStagePlan(
            slot_id=str(info["slot_id"]),
            account_id=int(info["account_id"]),
            brief=brief,
            reply_preview=str(info.get("reply_preview") or ""),
        ))
    return plans


def _mark_collapsed(plans: list[TwoStagePlan]) -> list[TwoStagePlan]:
    marked: list[TwoStagePlan] = []
    for plan in plans:
        if plan.brief is not None:
            marked.append(TwoStagePlan(
                slot_id=plan.slot_id,
                account_id=plan.account_id,
                rejection_code="batch_style_collapse",
                rejection_detail="重规划后同批 speech_act/长度/标点仍全部相同",
                reply_preview=plan.reply_preview,
            ))
        else:
            marked.append(plan)
    return marked


def load_voice_profile(session: Session, tenant_id: int, account_id: int) -> dict:
    """按账号加载当前 active 面具并派生声线合同 v3；无面具时使用默认档位。"""
    if not account_id:
        return voice_contract_v3(None)
    mask = session.scalar(
        select(AiAccountVoiceProfile)
        .where(
            AiAccountVoiceProfile.tenant_id == tenant_id,
            AiAccountVoiceProfile.account_id == account_id,
            AiAccountVoiceProfile.status == "active",
        )
        .order_by(AiAccountVoiceProfile.version.desc())
        .limit(1)
    )
    return voice_contract_v3(mask)


def realize_message_content(
    session: Session,
    tenant_id: int,
    config: dict,
    plan: TwoStagePlan,
    *,
    history_lines: list[str],
    rejection_feedback: str = "",
    realizer: BriefRealizer | None = None,
    reviewer: SemanticReviewer | None = None,
) -> tuple[str, dict, int]:
    if plan.brief is None:
        raise TwoStageRealizeError("brief_missing")
    if reviewer is None:
        _validate_default_reviewer_config(config)
    content, meta, tokens, voice, facts = _realize_draft(
        session, tenant_id, config, plan=plan, history_lines=history_lines,
        rejection_feedback=rejection_feedback,
        realizer=realizer or _default_realizer,
    )
    lexical_evidence = _validate_lexical_grounding(
        content, plan, meta, facts, tokens=tokens,
    )
    semantic_review, review_tokens = _review_realized_content(
        session, tenant_id, config, plan=plan, content=content,
        facts=facts, voice=voice, reviewer=reviewer or _default_reviewer,
        draft_tokens=tokens,
    )
    meta["lexical_grounding"] = lexical_evidence
    meta["semantic_review"] = semantic_review
    return content, meta, int(tokens or 0) + review_tokens


def _validate_lexical_grounding(
    content: str,
    plan: TwoStagePlan,
    meta: dict,
    facts: dict[str, str],
    *,
    tokens: int,
) -> dict:
    evidence = lexical_grounding_evidence(
        content,
        plan.brief,
        used_anchor_ids=list(meta["used_anchor_ids"]),
        anchor_texts=facts,
        reply_preview=plan.reply_preview,
    )
    if evidence.get("unsupported_claim_marker"):
        raise TwoStageRealizeError(
            "unsupported_claim",
            evidence={"lexical_grounding": evidence},
            tokens=int(tokens or 0),
        )
    return evidence


def _review_realized_content(
    session: Session,
    tenant_id: int,
    config: dict,
    *,
    plan: TwoStagePlan,
    content: str,
    facts: dict[str, str],
    voice: dict,
    reviewer: SemanticReviewer,
    draft_tokens: int,
) -> tuple[dict, int]:
    try:
        return _run_semantic_review(
            session, tenant_id, config, plan=plan, content=content,
            facts=facts, voice=voice, reviewer=reviewer,
        )
    except TwoStageRealizeError as exc:
        raise TwoStageRealizeError(
            exc.code,
            evidence=exc.evidence,
            tokens=int(draft_tokens or 0) + exc.tokens,
        ) from exc


def _realize_draft(
    session: Session,
    tenant_id: int,
    config: dict,
    *,
    plan: TwoStagePlan,
    history_lines: list[str],
    rejection_feedback: str,
    realizer: BriefRealizer,
) -> tuple[str, dict, int, dict, dict[str, str]]:
    voice = load_voice_profile(session, tenant_id, plan.account_id)
    facts = fact_id_map(history_lines)
    user_prompt = build_realizer_user_prompt(
        plan.brief,
        voice,
        anchor_texts=facts,
        reply_preview=plan.reply_preview,
        rejection_feedback=rejection_feedback,
    )
    payload, tokens = realizer(
        session, tenant_id, config,
        system_prompt=VOICE_REALIZER_SYSTEM_PROMPT, user_prompt=user_prompt,
    )
    item = payload[0] if isinstance(payload, list) and payload else payload
    try:
        content, meta = parse_realizer_response(item, plan.brief)
    except ValueError as exc:
        raise TwoStageRealizeError(str(exc)) from exc
    return content, meta, int(tokens or 0), voice, facts


def _run_semantic_review(
    session: Session,
    tenant_id: int,
    config: dict,
    *,
    plan: TwoStagePlan,
    content: str,
    facts: dict[str, str],
    voice: dict,
    reviewer: SemanticReviewer,
) -> tuple[dict, int]:
    payload, tokens = reviewer(
        session,
        tenant_id,
        config,
        system_prompt=SEMANTIC_REVIEW_SYSTEM_PROMPT,
        user_prompt=_semantic_review_prompt(plan, content, facts=facts, voice=voice),
    )
    evidence = _parse_semantic_review(payload, config)
    evidence["candidate_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    decision = evidence["decision"]
    confidence = evidence["confidence"]
    codes = evidence["codes"]
    if decision == "pass" and codes:
        raise TwoStageRealizeError(
            codes[0], evidence=evidence, tokens=int(tokens or 0),
        )
    if decision == "pass" and confidence >= SEMANTIC_REVIEW_CONFIDENCE_THRESHOLD:
        return evidence, int(tokens or 0)
    if confidence < SEMANTIC_REVIEW_CONFIDENCE_THRESHOLD or decision == "uncertain":
        raise TwoStageRealizeError(
            "semantic_review_uncertain",
            evidence=evidence,
            tokens=int(tokens or 0),
        )
    code = next(iter(evidence["codes"]), "semantic_review_failed")
    raise TwoStageRealizeError(code, evidence=evidence, tokens=int(tokens or 0))


def _semantic_review_prompt(
    plan: TwoStagePlan,
    content: str,
    *,
    facts: dict[str, str],
    voice: dict,
) -> str:
    brief = plan.brief
    payload = {
        "brief": brief.to_payload() if brief else {},
        "allowed_facts": {key: facts[key] for key in (brief.anchor_ids if brief else ()) if key in facts},
        "reply_preview": plan.reply_preview,
        "voice_profile": voice,
        "candidate": content,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _parse_semantic_review(payload: object, config: dict) -> dict:
    item = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(item, dict):
        raise TwoStageRealizeError("semantic_review_schema_invalid")
    decision = str(item.get("decision") or "").strip()
    confidence = item.get("confidence")
    evidence = item.get("evidence")
    prompt_version = str(item.get("prompt_version") or "").strip()
    if decision not in {"pass", "fail", "uncertain"}:
        raise TwoStageRealizeError("semantic_review_schema_invalid")
    if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        raise TwoStageRealizeError("semantic_review_confidence_invalid")
    if not isinstance(evidence, list) or not evidence:
        raise TwoStageRealizeError("semantic_review_evidence_missing")
    if any(
        not isinstance(item, dict)
        or not str(item.get("criterion") or "").strip()
        or not str(item.get("observed") or "").strip()
        for item in evidence
    ):
        raise TwoStageRealizeError("semantic_review_evidence_invalid")
    if prompt_version != SEMANTIC_REVIEW_PROMPT_VERSION:
        raise TwoStageRealizeError("semantic_review_prompt_version_mismatch")
    return {
        "decision": decision,
        "confidence": float(confidence),
        "codes": [str(code) for code in (item.get("codes") or []) if str(code)],
        "evidence": evidence,
        "prompt_version": prompt_version,
        "model": str(config.get("ai_semantic_reviewer_model") or "injected_test_reviewer"),
    }


def comment_history_lines(payload) -> list[str]:
    """评论两阶段的事实锚点来源：频道原文 + 引用目标预览。"""
    lines = [str(payload.message_content or "")]
    if getattr(payload, "reply_to_message_id", ""):
        lines.append(str(getattr(payload, "reply_target_preview", "") or ""))
    return [line for line in lines if line.strip()]


__all__ = [
    "QUALITY_WAIT",
    "TWO_STAGE_FLAG",
    "TWO_STAGE_REALIZE_ATTEMPTS",
    "TwoStagePlan",
    "TwoStageRealizeError",
    "comment_history_lines",
    "load_voice_profile",
    "plan_message_briefs",
    "realize_message_content",
    "two_stage_enabled",
]
