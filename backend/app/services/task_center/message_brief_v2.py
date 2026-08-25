from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .message_brief import LENGTH_BANDS, STANCES, MessageBrief, parse_brief_item


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
CLAIM_SPEECH_ACTS = {
    "grounded_reaction": "reaction",
    "fact_question": "question",
    "agreement": "agreement",
    "adult_visual_reaction": "reaction",
    "adult_visual_question": "question",
    "adult_product_reaction": "reaction",
    "adult_product_question": "question",
    **{category: "question" for category in QUESTION_ONLY_CLAIMS},
    "sensory_reaction": "reaction",
    "sensory_question": "question",
}
_NUMBER = r"(?:\d+(?:\.\d{1,2})?|[零〇一二两三四五六七八九十百千万]+)"
_EXACT_PRICE = re.compile(rf"(?:¥|￥)?{_NUMBER}(?:元|块钱|块)")
_CONTACT = re.compile(r"(?:微信|vx|v信|电话|手机号|TG|Telegram)[:：]?\s*[A-Za-z0-9_+-]{5,}", re.I)
_WRONG_SENSORY_OBJECT = re.compile(r"(?:衣服|裙子|裤子|布料).{0,4}(?:润|湿|水多)")
_ADULT_MARKERS = (
    "好润",
    "水多",
    "湿不湿",
    "身材",
    "胸",
    "嘴唇",
    "写真",
    "性感",
    "黑丝",
    "丝袜",
    "高跟鞋",
    "成人用品",
    "情趣用品",
    "跳蛋",
    "飞机杯",
    "按摩棒",
    "怎么约",
    "能约",
    "可约",
    "讲课费",
    "多少钱",
    "包夜",
    "上门",
)
_ADULT_CUTESY_MARKERS = (
    "软软的",
    "水灵灵",
    "好心动",
    "挺好看的",
    "真不错",
)
_SENSORY_INTENT_MARKERS = ("润", "水多", "水量", "水滋滋", "湿不湿", "润不润")
_SENSORY_REACTIONS = frozenset({"好润", "真润", "够润", "水滋滋"})
_SENSORY_QUESTIONS = frozenset({"水多不？", "润不润？", "湿不湿？"})
_SENSORY_VAGUE_EXPRESSIONS = frozenset({"水润感"})
_UNSUPPORTED_ASSERTIONS = {
    "experience": (
        "我去过",
        "我用过",
        "我试过",
        "亲自体验",
        "刚体验完",
        "上次去",
        "前天刚去过",
        "昨天见过",
    ),
    "transaction": ("我下单", "我付款", "已经买", "刚买", "成交了", "花钱找气受"),
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
    negative_phrases: tuple[str, ...] = ()
    prompt_contract_version: str = "general_v3"
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
            "negative_phrases": list(self.negative_phrases),
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
    negative_phrases: tuple[str, ...] = ()


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
    claims = _parse_claims(
        item.get("claims"),
        contract,
        valid_fact_ids,
        expected_speech_act=base.speech_act,
    )
    if not claims or not _brief_shape_matches(base, claims, contract):
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
        negative_phrases=contract.negative_phrases,
        prompt_contract_version=contract.prompt_contract_version,
        example_set_version=contract.example_set_version,
    )


def build_v2_planner_prompt(
    *,
    slot_infos: list[dict],
    allowed_facts: list[dict],
    recent_briefs: list[dict] | None = None,
) -> str:
    payload = {
        "slots": [_planner_input_slot(slot) for slot in slot_infos],
        "allowed_facts": allowed_facts,
        "recent_brief_shapes": list(recent_briefs or ()),
    }
    output = {"briefs": [_planner_output_slot(slot) for slot in slot_infos]}
    return (
        "输入：" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n输出合同（替换占位符）："
        + json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    )


def _planner_input_slot(slot: dict) -> dict:
    mode = str(slot.get("content_mode") or "")
    claims = sorted(MODE_CLAIMS.get(mode, ()))
    return {
        "slot_id": str(slot.get("slot_id") or ""),
        "reply_to_message_id": str(slot.get("reply_to_message_id") or ""),
        "reply_preview": str(slot.get("reply_preview") or ""),
        "context_route": str(slot.get("context_route") or ""),
        "content_mode": mode,
        "route_evidence_ids": list(slot.get("route_evidence_ids") or ()),
        "allowed_claims": [
            {"category": category, "speech_act": CLAIM_SPEECH_ACTS[category]}
            for category in claims
        ],
        "allowed_length_bands": list(_mode_length_bands(mode)),
    }


def _planner_output_slot(slot: dict) -> dict:
    return {
        "slot_id": str(slot.get("slot_id") or ""),
        "speech_act": "<allowed_claims.speech_act>",
        "stance": "<" + "|".join(STANCES) + ">",
        "length_band": "<allowed_length_bands 中一项>",
        "punctuation_profile": "<question iff speech_act=question; otherwise none|pause>",
        "anchor_ids": ["<route_evidence_ids 中至少一项>"],
        "reply_to_message_id": str(slot.get("reply_to_message_id") or ""),
        "claims": [{
            "category": "<allowed_claims.category>",
            "speech_act": "<与外层 speech_act 相同>",
            "evidence_ids": ["<与 anchor_ids 相同或其子集>"],
        }],
    }


def _mode_length_bands(mode: str) -> tuple[str, ...]:
    return ("micro", "short") if mode in ADULT_MODES else LENGTH_BANDS


def v2_planner_system_prompt() -> str:
    return (
        "你只规划 Telegram 短消息，不写正文。只输出唯一 JSON 根对象 briefs，数量和顺序必须与 slots 一致。"
        "每个 claim 从当前 slot.allowed_claims 选一项；内外 speech_act 相同，anchor/evidence 只用该 slot 的"
        "route_evidence_ids，reply_to_message_id 原样复制。问句必须 question 标点，其他不得用 question。"
        "服务端持有 route、hash 和版本，输出不得复制或改写这些冻结字段。禁止补金额、地址、联系方式或经历。"
    )


def v2_realizer_system_prompt(brief: MessageBriefV2) -> str:
    mode_rules = {
        "general": "只接普通事实；像群友随手说，不总结、不精致点评、不强转成人。",
        "adult_visual": "只接成年视觉事实；短、直、粗粝，可参考真美、好润、水滋滋，禁止甜宠审美腔。",
        "adult_product": "只接一个成人用品点；像买家短问，禁止空泛夸赞或编使用经历。",
        "adult_service_inquiry": "只问 brief claim 指定的一类服务信息，不作断言，不加称呼或寒暄。",
        "adult_service_sensory": _sensory_realizer_rule(brief),
    }
    claim_rule = _service_claim_realizer_rule(brief)
    length_rule = {
        "micro": "正文1到8字",
        "short": "正文9到24字",
        "medium": "正文至少25字",
    }[brief.length_band]
    punctuation_rule = _punctuation_realizer_rule(brief)
    return (
        "把已审核 brief 写成一条自然中文 Telegram 消息。"
        f"{mode_rules[brief.content_mode]}{claim_rule}{length_rule}；{punctuation_rule}"
        "只用 evidence，保持 brief 的 speech_act；"
        "禁止软软的、水灵灵的、好心动、挺好看的、这状态真不错；"
        "禁止老板、老师、早上好、上午好、晚上好等称呼寒暄；"
        "输出 content、used_anchor_ids、speech_act、voice_profile_version JSON。"
    )


def _punctuation_realizer_rule(brief: MessageBriefV2) -> str:
    rules = {
        "question": "正文必须以问号结尾，不得使用逗号、顿号、分号或省略号；",
        "pause": "正文至少包含一个逗号、顿号、分号或省略号，且不得含问号；",
        "none": "正文不得含问号、逗号、顿号、分号或省略号；",
    }
    return rules[brief.punctuation_profile]


def _sensory_realizer_rule(brief: MessageBriefV2) -> str:
    if brief.speech_act == "question":
        examples = "、".join(sorted(_SENSORY_QUESTIONS))
        return f"围绕 evidence 写一句短问，可参考{examples}的直接感，但要自然变化，不能固定照抄。"
    examples = "、".join(sorted(_SENSORY_REACTIONS))
    return f"围绕 evidence 写一句短反应，可参考{examples}的直接感，但要自然变化，不能固定照抄。"


def _service_claim_realizer_rule(brief: MessageBriefV2) -> str:
    if brief.content_mode != "adult_service_inquiry" or not brief.claims:
        return ""
    rules = {
        "price_question": "只问价格；",
        "region_question": "只问区域；",
        "availability_question": "只问现在或今天是否有空；",
        "service_question": "只问有什么项目或都做什么；",
        "duration_question": "只问时长；",
        "identity_question": "只问是否本人；",
        "booking_question": "只问怎么预约；",
    }
    rule = rules.get(brief.claims[0].category, "")
    return f"{rule}必须以问号结尾；" if rule else ""


def v2_candidate_failure(
    content: str,
    brief: MessageBriefV2,
    evidence_texts: tuple[str, ...] = (),
) -> str:
    if any(phrase in content for phrase in brief.negative_phrases):
        return "negative_lexicon_match"
    if _EXACT_PRICE.search(content):
        return "unsupported_claim"
    if _CONTACT.search(content):
        return "unsupported_claim"
    if _unsupported_assertion(content, evidence_texts):
        return "unsupported_claim"
    if brief.content_mode == "general" and any(marker in content for marker in _ADULT_MARKERS):
        return "general_forced_adult"
    if brief.content_mode in ADULT_MODES and any(
        marker in content for marker in _ADULT_CUTESY_MARKERS
    ):
        return "adult_cutesy_tone"
    if brief.content_mode == "adult_service_sensory" and _WRONG_SENSORY_OBJECT.search(content):
        return "sensory_object_wrong"
    if brief.content_mode == "adult_service_sensory" and content in _SENSORY_VAGUE_EXPRESSIONS:
        return "sensory_expression_vague"
    if brief.content_mode == "adult_service_sensory" and not any(
        marker in content for marker in _SENSORY_INTENT_MARKERS
    ):
        return "sensory_intent_missing"
    if brief.content_mode == "adult_service_sensory":
        is_question = content.endswith(("?", "？"))
        if (brief.speech_act == "question") != is_question:
            return "sensory_speech_act_mismatch"
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
    evidence_texts: tuple[str, ...],
) -> bool:
    evidence = " ".join(evidence_texts)
    for markers in _UNSUPPORTED_ASSERTIONS.values():
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
    for key, value in expected.items():
        if key in item and str(item.get(key) or "") != value:
            return False
    if "route_evidence_ids" not in item:
        return True
    returned = tuple(str(value) for value in (item.get("route_evidence_ids") or ()))
    return returned == contract.route_evidence_ids


def _parse_claims(
    value: object,
    contract: V2BriefContract,
    valid_fact_ids: tuple[str, ...],
    *,
    expected_speech_act: str,
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
    if speech_act != expected_speech_act or CLAIM_SPEECH_ACTS.get(category) != speech_act:
        return ()
    if not evidence_ids or any(item not in valid_fact_ids for item in evidence_ids):
        return ()
    if any(item not in contract.route_evidence_ids for item in evidence_ids):
        return ()
    if category in QUESTION_ONLY_CLAIMS and speech_act != "question":
        return ()
    return (GroundedClaim(category, speech_act, evidence_ids),)


def _brief_shape_matches(
    brief: MessageBrief,
    claims: tuple[GroundedClaim, ...],
    contract: V2BriefContract,
) -> bool:
    question = brief.speech_act == "question"
    if (brief.punctuation_profile == "question") != question:
        return False
    if any(anchor not in contract.route_evidence_ids for anchor in brief.anchor_ids):
        return False
    if any(evidence not in brief.anchor_ids for claim in claims for evidence in claim.evidence_ids):
        return False
    if contract.content_mode in ADULT_MODES and brief.length_band not in {"micro", "short"}:
        return False
    return True


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
