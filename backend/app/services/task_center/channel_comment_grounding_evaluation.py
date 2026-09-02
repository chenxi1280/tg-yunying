from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelCommentGroundingAssignment,
    ChannelCommentGroundingEvaluation,
    ChannelCommentGroundingSnapshot,
)

from .channel_payloads import PostCommentPayload


DETERMINISTIC_EVALUATOR_VERSION = "channel_comment_claim_evaluator_v1"
SEMANTIC_REVIEWER_SCHEMA_VERSION = "channel_comment_semantic_review_v1"
EXPERIENCE_PATTERN = re.compile(r"我(?:去过|体验过|试过|约过|见过)|亲测|上次|之前去")
NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?(?:cm|厘米|米|元|折|点|号)?", re.IGNORECASE)
CONTACT_PATTERN = re.compile(r"(?:https?://|www\.)\S+|@\w+|\b1\d{10}\b", re.IGNORECASE)
TIME_PATTERN = re.compile(r"今日|今天|当天|下午|今晚|今夜|明天|\d{1,2}[点时]")
LOCATION_PATTERN = re.compile(
    r"天河|越秀|海珠|白云|番禺|南山|福田|罗湖|宝安|龙华|龙岗|朝阳|海淀|丰台|西城|东城|武侯|锦江|成华|青羊|高新|金水|二七|管城|郑东|小寨|雁塔|碑林|南稍门|和平|滨江道|南开|河西|河东",
)
SERVICE_PATTERN = re.compile(
    r"水疗|按摩|SPA|服务|项目|手法|配合度|口活|漫游|不机车|不催钟|态度",
    re.IGNORECASE,
)
OFFER_PATTERN = re.compile(r"活动|优惠|特惠|折扣|立减|福利|折后")
TEACHER_PATTERN = re.compile(r"([\w\u4e00-\u9fff·]{1,12})老师")


@dataclass(frozen=True)
class GroundingClaimDecision:
    allowed: bool
    code: str
    detail: str
    claim_results: tuple[dict, ...]


def evaluate_grounding_claims(
    session: Session,
    action: Action,
    payload: PostCommentPayload,
    *,
    content: str,
) -> GroundingClaimDecision:
    if not payload.grounding_enrollment_id:
        return GroundingClaimDecision(True, "", "", ())
    assignment = session.get(
        ChannelCommentGroundingAssignment, payload.grounding_assignment_id,
    )
    snapshot = session.get(
        ChannelCommentGroundingSnapshot, payload.grounding_snapshot_id,
    )
    if assignment is None or snapshot is None or assignment.tenant_id != action.tenant_id:
        return GroundingClaimDecision(
            False, "grounding_contract_stale", "Grounding snapshot/assignment 不存在", (),
        )
    claims = _extract_claims(content)
    results = tuple(
        _map_claim(claim, assignment=assignment, snapshot=snapshot)
        for claim in claims
    )
    rejected = next((row for row in results if row["result"] != "pass"), None)
    if rejected is None:
        return GroundingClaimDecision(True, "", "", results)
    code = "unsupported_teacher" if rejected["claim_type"] == "teacher" else "unsupported_claim"
    return GroundingClaimDecision(False, code, str(rejected["reason_code"]), results)


def persist_grounding_evaluation(
    session: Session,
    action: Action,
    payload: PostCommentPayload,
    *,
    candidate_hash: str,
    claim_results: list[dict],
    semantic_evidence: dict,
    final_result: str,
) -> ChannelCommentGroundingEvaluation | None:
    if not payload.grounding_enrollment_id or not candidate_hash:
        return None
    attempt_id = str(payload.ai_generation_attempt_id or "")
    existing = _existing_evaluation(
        session, action.id, attempt_id=attempt_id, candidate_hash=candidate_hash,
    )
    if existing is not None:
        return existing
    semantic_decision = str(semantic_evidence.get("decision") or "unknown")
    primary = _review_dimension(
        semantic_evidence, "primary_aspect_result", semantic_decision,
    )
    relation = (
        _review_dimension(
            semantic_evidence, "reply_relation_result", semantic_decision,
        )
        if payload.comment_mode == "reply"
        else "not_applicable"
    )
    resolved_final = _final_evaluation_result(
        requested=final_result,
        claim_results=claim_results,
        primary=primary,
        relation=relation,
    )
    evaluation = _new_evaluation(
        action,
        payload,
        attempt_id=attempt_id,
        candidate_hash=candidate_hash,
        claim_results=claim_results,
        semantic_evidence=semantic_evidence,
        primary=primary,
        relation=relation,
        final_result=resolved_final,
    )
    session.add(evaluation)
    session.flush()
    return evaluation


def _existing_evaluation(
    session: Session,
    action_id: str,
    *,
    attempt_id: str,
    candidate_hash: str,
) -> ChannelCommentGroundingEvaluation | None:
    return session.scalar(select(ChannelCommentGroundingEvaluation).where(
        ChannelCommentGroundingEvaluation.action_id == action_id,
        ChannelCommentGroundingEvaluation.generation_attempt_id == attempt_id,
        ChannelCommentGroundingEvaluation.candidate_content_hash == candidate_hash,
    ))


def _new_evaluation(
    action: Action,
    payload: PostCommentPayload,
    *,
    attempt_id: str,
    candidate_hash: str,
    claim_results: list[dict],
    semantic_evidence: dict,
    primary: str,
    relation: str,
    final_result: str,
) -> ChannelCommentGroundingEvaluation:
    return ChannelCommentGroundingEvaluation(
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        action_id=action.id,
        generation_job_id=str(payload.generation_job_id or "") or None,
        generation_attempt_id=attempt_id,
        candidate_content_hash=candidate_hash,
        deterministic_evaluator_version=DETERMINISTIC_EVALUATOR_VERSION,
        semantic_reviewer_request_id=f"{payload.ai_generation_request_id}:semantic-review",
        semantic_reviewer_model=str(semantic_evidence.get("model") or ""),
        semantic_reviewer_schema_version=SEMANTIC_REVIEWER_SCHEMA_VERSION,
        semantic_reviewer_prompt_version=str(semantic_evidence.get("prompt_version") or ""),
        semantic_reviewer_input_hash=_input_hash(payload, candidate_hash),
        claim_results_json=list(claim_results),
        primary_aspect_result=primary,
        reply_relation_result=relation,
        final_result=final_result,
    )


def _extract_claims(content: str) -> list[dict]:
    rows = []
    for claim_type, pattern in (
        ("experience", EXPERIENCE_PATTERN),
        ("contact", CONTACT_PATTERN),
        ("number", NUMBER_PATTERN),
        ("time", TIME_PATTERN),
        ("location", LOCATION_PATTERN),
        ("service", SERVICE_PATTERN),
        ("offer", OFFER_PATTERN),
        ("teacher", TEACHER_PATTERN),
    ):
        for match in pattern.finditer(content):
            rows.append({
                "claim_id": f"claim-{len(rows) + 1}",
                "claim_type": claim_type,
                "text": match.group(0),
                "text_span": [match.start(), match.end()],
            })
    return sorted(rows, key=lambda row: (row["text_span"][0], row["claim_type"]))


def _map_claim(
    claim: dict,
    *,
    assignment: ChannelCommentGroundingAssignment,
    snapshot: ChannelCommentGroundingSnapshot,
) -> dict:
    supported_ids = _supporting_evidence_ids(claim, assignment=assignment, snapshot=snapshot)
    if claim["claim_type"] == "experience":
        supported_ids = []
        reason = "unsupported_personal_experience"
    elif supported_ids:
        reason = ""
    else:
        reason = f"unsupported_{claim['claim_type']}"
    return {
        **claim,
        "supported_evidence_ids": supported_ids,
        "result": "pass" if supported_ids else "reject",
        "reason_code": reason,
    }


def _supporting_evidence_ids(
    claim: dict,
    *,
    assignment: ChannelCommentGroundingAssignment,
    snapshot: ChannelCommentGroundingSnapshot,
) -> list[str]:
    if claim["claim_type"] == "teacher":
        normalized = str(claim["text"]).removesuffix("老师")
        return (
            [assignment.primary_evidence_id]
            if normalized == assignment.teacher_name.removesuffix("老师")
            and assignment.teacher_candidate_id
            else []
        )
    allowed_ids = {
        assignment.primary_evidence_id,
        assignment.secondary_evidence_id,
    } - {""}
    return [
        str(row["evidence_id"])
        for row in snapshot.aspect_evidence_json
        if row.get("evidence_id") in allowed_ids
        and str(claim["text"]).casefold() in str(row.get("source_text") or "").casefold()
    ]


def _semantic_failure(decision: str) -> str:
    return "reject" if decision == "fail" else "unknown"


def _review_dimension(evidence: dict, key: str, decision: str) -> str:
    explicit = str(evidence.get(key) or "")
    if explicit in {"pass", "reject", "unknown"}:
        return explicit
    return "pass" if decision == "pass" else _semantic_failure(decision)


def _final_evaluation_result(
    *,
    requested: str,
    claim_results: list[dict],
    primary: str,
    relation: str,
) -> str:
    if any(row.get("result") != "pass" for row in claim_results):
        return "reject"
    if requested != "pass":
        return "reject" if requested == "reject" else "unknown"
    dimensions = {primary, relation} - {"not_applicable"}
    if "reject" in dimensions:
        return "reject"
    return "pass" if dimensions == {"pass"} else "unknown"


def _input_hash(payload: PostCommentPayload, candidate_hash: str) -> str:
    identity = {
        "snapshot_id": payload.grounding_snapshot_id,
        "assignment_id": payload.grounding_assignment_id,
        "candidate_hash": candidate_hash,
        "reply_target": payload.reply_to_message_id,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "DETERMINISTIC_EVALUATOR_VERSION",
    "GroundingClaimDecision",
    "evaluate_grounding_claims",
    "persist_grounding_evaluation",
]
