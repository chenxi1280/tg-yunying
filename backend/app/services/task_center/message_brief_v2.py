from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .message_brief import MessageBrief, parse_brief_item


BRIEF_V2 = "message_brief_v2"
GENERAL_MODES = frozenset({"general"})
ADULT_MODES = frozenset({
    "adult_visual",
    "adult_product",
    "adult_service_inquiry",
    "adult_service_sensory",
})
MODE_CLAIMS = {
    "general": frozenset({"grounded_reaction", "fact_question", "agreement"}),
    "adult_visual": frozenset({"adult_visual_reaction", "adult_visual_question"}),
    "adult_product": frozenset({"adult_product_reaction", "adult_product_question"}),
    "adult_service_inquiry": frozenset({
        "price_question",
        "region_question",
        "availability_question",
        "service_question",
        "duration_question",
        "identity_question",
        "booking_question",
    }),
    "adult_service_sensory": frozenset({"sensory_reaction", "sensory_question"}),
}
QUESTION_ONLY_CLAIMS = frozenset(MODE_CLAIMS["adult_service_inquiry"])
_NUMBER = r"(?:\d+(?:\.\d{1,2})?|[零〇一二两三四五六七八九十百千万]+)"
_EXACT_PRICE = re.compile(rf"(?:¥|￥)?{_NUMBER}(?:元|块钱|块)")
_CONTACT = re.compile(r"(?:微信|vx|v信|电话|手机号|TG|Telegram)[:：]?\s*[A-Za-z0-9_+-]{5,}", re.I)
_WRONG_SENSORY_OBJECT = re.compile(r"(?:衣服|裙子|裤子|布料).{0,4}(?:润|湿|水多)")
_ADULT_MARKERS = ("好润", "水多", "湿不湿", "多少钱", "包夜", "上门")
_UNSUPPORTED_ASSERTIONS = {
    "experience": ("我去过", "我用过", "我试过", "亲自体验", "上次去"),
    "transaction": ("我下单", "我付款", "已经买", "刚买", "成交了"),
    "location": ("地址是", "就在我这", "在我这边", "具体地址"),
}
_QUESTION_MARKERS = {
    "price_question": ("多少", "价格", "收费", "价", "钱"),
    "region_question": ("哪里", "哪儿", "哪个区", "位置", "地方"),
    "availability_question": ("有空", "空不", "什么时候", "在不在", "能不能"),
    "service_question": ("项目", "服务", "做什么", "有什么", "怎么玩"),
    "duration_question": ("多久", "多长", "几小时", "时间"),
    "identity_question": ("本人", "真人", "是你", "照片"),
    "booking_question": ("怎么约", "预约", "能约", "约吗", "约不"),
}


@dataclass(frozen=True)
class GroundedClaim:
    category: str
    speech_act: str
    evidence_ids: tuple[str, ...]

    def to_payload(self) -> dict:
        return {
            "category": self.category,
            "speech_act": self.speech_act,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class MessageBriefV2(MessageBrief):
    task_direction_snapshot_hash: str = ""
    content_policy_hash: str = ""
    window_plan_hash: str = ""
    context_route: str = "general"
    content_mode: str = "general"
    route_evidence_ids: tuple[str, ...] = ()
    claims: tuple[GroundedClaim, ...] = ()
    forbidden_claim_categories: tuple[str, ...] = ()
    prompt_contract_version: str = "general_v2"
    example_set_version: str = ""
    brief_contract_version: str = BRIEF_V2

    def to_payload(self) -> dict:
        return {
            **super().to_payload(),
            "task_direction_snapshot_hash": self.task_direction_snapshot_hash,
            "content_policy_hash": self.content_policy_hash,
            "window_plan_hash": self.window_plan_hash,
            "context_route": self.context_route,
            "content_mode": self.content_mode,
            "route_evidence_ids": list(self.route_evidence_ids),
            "claims": [item.to_payload() for item in self.claims],
            "forbidden_claim_categories": list(self.forbidden_claim_categories),
            "prompt_contract_version": self.prompt_contract_version,
            "example_set_version": self.example_set_version,
        }


@dataclass(frozen=True)
class V2BriefContract:
    task_direction_snapshot_hash: str
    content_policy_hash: str
    window_plan_hash: str
    context_route: str
    content_mode: str
    route_evidence_ids: tuple[str, ...]
    prompt_contract_version: str
    example_set_version: str
    forbidden_claim_categories: tuple[str, ...]


def parse_brief_v2_item(
    item: object,
    *,
    slot_id: str,
    valid_fact_ids: tuple[str, ...],
    contract: V2BriefContract,
) -> MessageBriefV2 | None:
    base = parse_brief_item(item, slot_id=slot_id, valid_fact_ids=valid_fact_ids)
    if base is None or not isinstance(item, dict):
        return None
    if not _contract_matches(item, contract):
        return None
    claims = _parse_claims(item.get("claims"), contract, valid_fact_ids)
    if not claims:
        return None
    return MessageBriefV2(
        **_base_values(base),
        task_direction_snapshot_hash=contract.task_direction_snapshot_hash,
        content_policy_hash=contract.content_policy_hash,
        window_plan_hash=contract.window_plan_hash,
        context_route=contract.context_route,
        content_mode=contract.content_mode,
        route_evidence_ids=contract.route_evidence_ids,
        claims=claims,
        forbidden_claim_categories=contract.forbidden_claim_categories,
        prompt_contract_version=contract.prompt_contract_version,
        example_set_version=contract.example_set_version,
    )


def build_v2_planner_prompt(
    *,
    slot_infos: list[dict],
    allowed_facts: list[dict],
) -> str:
    payload = {"slots": slot_infos, "allowed_facts": allowed_facts}
    return "只为每个 slot 输出一个有证据的 MessageBrief v2 JSON。\n" + json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def v2_planner_system_prompt() -> str:
    return (
        "你只规划 Telegram 短消息，不写正文。每个 claim 必须绑定 category、speech_act 和 exact evidence_ids；"
        "只用 slot 指定 route/mode，禁止补金额、地址、联系方式或经历。严格输出 briefs JSON。"
    )


def v2_realizer_system_prompt(brief: MessageBriefV2) -> str:
    mode_rules = {
        "general": "只接普通事实，不强转成人话题。",
        "adult_visual": "只接成年视觉事实，不虚构服务交易。",
        "adult_product": "只接一个成人用品事实。",
        "adult_service_inquiry": "只问一个已证实类别，不作价格地点服务断言。",
        "adult_service_sensory": "输出2到6字的感官反应或问题，不套模板。",
    }
    return (
        "把已审核 brief 写成一条自然中文 Telegram 消息。"
        f"{mode_rules[brief.content_mode]}只用 evidence，保持 brief 的 speech_act；"
        "输出 content、used_anchor_ids、speech_act、voice_profile_version JSON。"
    )


def v2_candidate_failure(
    content: str,
    brief: MessageBriefV2,
    evidence_texts: tuple[str, ...] = (),
) -> str:
    if _EXACT_PRICE.search(content):
        return "unsupported_claim"
    if _CONTACT.search(content):
        return "unsupported_claim"
    if _unsupported_assertion(content, brief, evidence_texts):
        return "unsupported_claim"
    if brief.content_mode == "general" and any(marker in content for marker in _ADULT_MARKERS):
        return "general_forced_adult"
    if brief.content_mode == "adult_service_sensory" and _WRONG_SENSORY_OBJECT.search(content):
        return "sensory_object_wrong"
    if any(claim.category in QUESTION_ONLY_CLAIMS for claim in brief.claims):
        if brief.speech_act != "question":
            return "claim_category_mismatch"
        if not (content.endswith("?") or content.endswith("？")):
            return "claim_category_mismatch"
        if not _question_claim_matches(content, brief.claims[0].category):
            return "claim_category_mismatch"
    return ""


def _unsupported_assertion(
    content: str,
    brief: MessageBriefV2,
    evidence_texts: tuple[str, ...],
) -> bool:
    evidence = " ".join(evidence_texts)
    forbidden = set(brief.forbidden_claim_categories)
    for category, markers in _UNSUPPORTED_ASSERTIONS.items():
        names = {category, f"{category}_assertion"}
        if forbidden and not names & forbidden:
            continue
        if any(marker in content and marker not in evidence for marker in markers):
            return True
    return False


def _question_claim_matches(content: str, category: str) -> bool:
    markers = _QUESTION_MARKERS.get(category, ())
    return bool(markers and any(marker in content for marker in markers))


def _contract_matches(item: dict, contract: V2BriefContract) -> bool:
    expected = {
        "brief_contract_version": BRIEF_V2,
        "task_direction_snapshot_hash": contract.task_direction_snapshot_hash,
        "content_policy_hash": contract.content_policy_hash,
        "window_plan_hash": contract.window_plan_hash,
        "context_route": contract.context_route,
        "content_mode": contract.content_mode,
        "prompt_contract_version": contract.prompt_contract_version,
        "example_set_version": contract.example_set_version,
    }
    return all(str(item.get(key) or "") == value for key, value in expected.items())


def _parse_claims(
    value: object,
    contract: V2BriefContract,
    valid_fact_ids: tuple[str, ...],
) -> tuple[GroundedClaim, ...]:
    if not isinstance(value, list) or len(value) != 1:
        return ()
    item = value[0]
    if not isinstance(item, dict):
        return ()
    category = str(item.get("category") or "")
    speech_act = str(item.get("speech_act") or "")
    evidence_ids = tuple(str(raw) for raw in (item.get("evidence_ids") or ()))
    if category not in MODE_CLAIMS.get(contract.content_mode, frozenset()):
        return ()
    if not evidence_ids or any(item not in valid_fact_ids for item in evidence_ids):
        return ()
    if any(item not in contract.route_evidence_ids for item in evidence_ids):
        return ()
    if category in QUESTION_ONLY_CLAIMS and speech_act != "question":
        return ()
    return (GroundedClaim(category, speech_act, evidence_ids),)


def _base_values(brief: MessageBrief) -> dict:
    return {
        "slot_id": brief.slot_id,
        "speech_act": brief.speech_act,
        "stance": brief.stance,
        "length_band": brief.length_band,
        "punctuation_profile": brief.punctuation_profile,
        "anchor_ids": brief.anchor_ids,
        "allowed_facts": brief.allowed_facts,
        "forbidden_claims": brief.forbidden_claims,
        "reply_to_message_id": brief.reply_to_message_id,
        "voice_profile_version": brief.voice_profile_version,
    }


__all__ = [
    "BRIEF_V2",
    "MessageBriefV2",
    "V2BriefContract",
    "build_v2_planner_prompt",
    "parse_brief_v2_item",
    "v2_candidate_failure",
    "v2_planner_system_prompt",
    "v2_realizer_system_prompt",
]
