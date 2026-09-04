from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .ai_provider_routes import route_v2_enabled
from .message_brief import (
    BRIEF_PLANNER_SYSTEM_PROMPT,
    MessageBrief,
    batch_style_collapse_reason,
    build_brief_planner_user_prompt,
    parse_brief_item,
)
from .message_brief_v2 import (
    V2BriefContract,
    build_v2_planner_prompt,
    parse_brief_v2_item,
    v2_planner_system_prompt,
)


@dataclass(frozen=True)
class TwoStagePlan:
    slot_id: str
    account_id: int = 0
    brief: MessageBrief | None = None
    rejection_code: str = ""
    rejection_detail: str = ""
    reply_preview: str = ""


def plan_message_briefs_with(
    session: Session,
    tenant_id: int,
    config: dict,
    *,
    history_facts: dict[str, str],
    slots: list[dict],
    planner: Callable,
) -> tuple[list[TwoStagePlan], int]:
    fact_entries = [
        {"fact_id": fact_id, "text": text}
        for fact_id, text in history_facts.items()
    ]
    slot_infos = _slot_infos(slots)
    use_v2 = route_v2_enabled(config)
    preflight = _v2_preflight_rejection(slot_infos, tuple(history_facts)) if use_v2 else None
    if preflight:
        return _reject_v2_slots(slot_infos, *preflight), 0
    total_tokens = 0
    recent: list[dict] = []
    plans: list[TwoStagePlan] = []
    for attempt_index in range(1, 3):
        prompt = _planner_prompt(slot_infos, fact_entries, recent, use_v2=use_v2)
        payload, tokens = planner(
            session,
            tenant_id,
            _planner_config(config, slot_infos, attempt_index),
            system_prompt=v2_planner_system_prompt() if use_v2 else BRIEF_PLANNER_SYSTEM_PROMPT,
            user_prompt=prompt,
            count=len(slot_infos),
        )
        total_tokens += int(tokens or 0)
        plans = _parse_plans(
            payload,
            slot_infos=slot_infos,
            fact_ids=tuple(history_facts),
            use_v2=use_v2,
        )
        briefs = [plan.brief for plan in plans if plan.brief is not None]
        if not batch_style_collapse_reason(briefs):
            return plans, total_tokens
        recent = [_shape(brief) for brief in briefs]
    return _mark_collapsed(plans), total_tokens


def _planner_config(config: dict, slot_infos: list[dict], attempt_index: int) -> dict:
    slot_ids = ",".join(str(item["slot_id"]) for item in slot_infos)
    invocation_key = f"planner:slots:{slot_ids}:attempt:{attempt_index}"
    planner_slots = [
        {
            "slot_id": item["slot_id"],
            "reply_to_message_id": item.get("reply_to_message_id") or "",
            "content_mode": item.get("content_mode") or "general",
            "route_evidence_ids": list(item.get("route_evidence_ids") or ()),
        }
        for item in slot_infos
    ]
    return {
        **config,
        "_ai_provider_invocation_key": invocation_key,
        "_provider_http_slot_ids": [item["slot_id"] for item in slot_infos] if config.get("generation_slots") else None,
        "_ai_provider_planner_slots": planner_slots,
    }


def _brief_items(payload: object) -> list[object]:
    if isinstance(payload, dict):
        for key in ("briefs", "items", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return [payload]
    return payload if isinstance(payload, list) else []


def _slot_infos(slots: list[dict]) -> list[dict]:
    return [
        {
            "slot_id": str(slot.get("slot_id") or ""),
            "account_id": int(slot.get("account_id") or 0),
            "reply_to_message_id": str(slot.get("reply_to_message_id") or ""),
            "reply_preview": str(slot.get("reply_preview") or "")[:120],
            **_v2_slot_info(slot),
        }
        for slot in slots
    ]


def _parse_plans(
    payload: object,
    *,
    slot_infos: list[dict],
    fact_ids: tuple[str, ...],
    use_v2: bool,
) -> list[TwoStagePlan]:
    items = _brief_items(payload)
    plans: list[TwoStagePlan] = []
    for index, info in enumerate(slot_infos):
        item = items[index] if index < len(items) else None
        brief = _parse_brief(item, info, fact_ids=fact_ids, use_v2=use_v2)
        rejection = _brief_rejection(brief, info)
        if rejection:
            plans.append(TwoStagePlan(
                slot_id=str(info["slot_id"]),
                account_id=int(info["account_id"]),
                rejection_code=rejection[0],
                rejection_detail=rejection[1],
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


def _brief_rejection(
    brief: MessageBrief | None,
    info: dict,
) -> tuple[str, str] | None:
    if brief is None:
        return "malformed_output", "brief_schema_invalid：schema、版本或证据引用非法"
    if brief.slot_id != str(info["slot_id"]):
        return "brief_slot_id_mismatch", "brief slot_id 与当前 slot 不一致"
    if brief.reply_to_message_id != str(info.get("reply_to_message_id") or ""):
        return "brief_reply_target_mismatch", "brief reply_to_message_id 与 slot 不一致"
    return None


def _parse_brief(
    item: object,
    info: dict,
    *,
    fact_ids: tuple[str, ...],
    use_v2: bool,
) -> MessageBrief | None:
    if not use_v2:
        return parse_brief_item(item, slot_id=str(info["slot_id"]), valid_fact_ids=fact_ids)
    contract = _v2_contract(info)
    if contract is None:
        return None
    return parse_brief_v2_item(
        item,
        slot_id=str(info["slot_id"]),
        valid_fact_ids=fact_ids,
        contract=contract,
    )


def _v2_contract(info: dict) -> V2BriefContract | None:
    required = (
        "task_direction_snapshot_hash",
        "content_policy_hash",
        "window_plan_hash",
        "context_route",
        "content_mode",
        "prompt_contract_version",
        "example_set_version",
    )
    if any(not str(info.get(key) or "") for key in required):
        return None
    evidence = tuple(str(item) for item in (info.get("route_evidence_ids") or ()))
    if not evidence:
        return None
    return V2BriefContract(
        task_direction_snapshot_hash=str(info["task_direction_snapshot_hash"]),
        content_policy_hash=str(info["content_policy_hash"]),
        window_plan_hash=str(info["window_plan_hash"]),
        context_route=str(info["context_route"]),
        content_mode=str(info["content_mode"]),
        route_evidence_ids=evidence,
        prompt_contract_version=str(info["prompt_contract_version"]),
        example_set_version=str(info["example_set_version"]),
        forbidden_claim_categories=tuple(
            str(item) for item in (info.get("forbidden_claim_categories") or ())
        ),
        negative_phrases=tuple(
            str(item) for item in (info.get("negative_phrases") or ())
        ),
    )


def _planner_prompt(
    slot_infos: list[dict],
    facts: list[dict],
    recent: list[dict],
    *,
    use_v2: bool,
) -> str:
    if use_v2:
        return build_v2_planner_prompt(
            slot_infos=slot_infos,
            allowed_facts=facts,
            recent_briefs=recent,
        )
    return build_brief_planner_user_prompt(
        slot_infos=slot_infos,
        allowed_facts=facts,
        recent_briefs=recent,
    )


def _v2_slot_info(slot: dict) -> dict:
    keys = (
        "task_direction_snapshot_hash",
        "content_policy_hash",
        "window_plan_hash",
        "context_route",
        "content_mode",
        "route_evidence_ids",
        "prompt_contract_version",
        "example_set_version",
        "forbidden_claim_categories",
        "negative_phrases",
    )
    return {key: slot.get(key) for key in keys}


def _v2_preflight_rejection(
    slot_infos: list[dict],
    fact_ids: tuple[str, ...],
) -> tuple[str, str] | None:
    valid = set(fact_ids)
    for info in slot_infos:
        contract = _v2_contract(info)
        if contract is None:
            return "brief_contract_invalid", "MessageBrief v2 冻结合同不完整"
        if any(evidence_id not in valid for evidence_id in contract.route_evidence_ids):
            return "brief_evidence_mismatch", "route evidence 不属于当前 allowed facts"
    return None


def _reject_v2_slots(
    slot_infos: list[dict],
    code: str,
    detail: str,
) -> list[TwoStagePlan]:
    return [
        TwoStagePlan(
            slot_id=str(info["slot_id"]),
            account_id=int(info["account_id"]),
            rejection_code=code,
            rejection_detail=detail,
            reply_preview=str(info.get("reply_preview") or ""),
        )
        for info in slot_infos
    ]


def _shape(brief: MessageBrief) -> dict:
    return {
        "slot_id": brief.slot_id,
        "speech_act": brief.speech_act,
        "length_band": brief.length_band,
        "punctuation_profile": brief.punctuation_profile,
    }


def _mark_collapsed(plans: list[TwoStagePlan]) -> list[TwoStagePlan]:
    return [
        TwoStagePlan(
            slot_id=plan.slot_id,
            account_id=plan.account_id,
            rejection_code="batch_style_collapse",
            rejection_detail="重规划后同批 speech_act/长度/标点仍全部相同",
            reply_preview=plan.reply_preview,
        ) if plan.brief is not None else plan
        for plan in plans
    ]


__all__ = ["TwoStagePlan", "plan_message_briefs_with"]
