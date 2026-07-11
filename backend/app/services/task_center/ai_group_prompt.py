from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


MAX_SAFE_MESSAGES = 5
DRAFT_KEYS = {
    "sequence_index",
    "reply_to_sequence_index",
    "persona",
    "content",
    "risk_level",
    "intent",
    "mood",
    "material_intent",
    "allow_material",
}
CLAUSE_SPLIT = re.compile(r"[，。！？；,.!?;]+")
LINE_PREFIX = re.compile(r"^[^:：\n]{1,40}[:：]\s*")
SPACE = re.compile(r"\s+")
FORBIDDEN = re.compile(
    r"(?:价格|价位|收费|费用|预算|套餐|付款|支付|转账|红包|多少钱|多少米|便宜|"
    r"微信|联系方式|联系|私聊|私信|加我|用户名|telegram|\btg\b|@[A-Za-z0-9_]{3,}|"
    r"地址|位置|定位|酒店|宾馆|房间|开房|上门|到店|预约|预订|安排|档期|可约|"
    r"资源|求推荐|有没有好|上牌|上车|服务|色情|寻欢客|妹妹|小妹|技师|楼凤|外围|全套|半套|"
    r"口活|无套口|做爱|性交|约炮|招嫖|嫖客|包养|面付|抓龙筋|大蟒蛇|可外|可包时|"
    r"未成年|学生妹|学生辈|少女|幼女|小女孩|好嫩|很嫩|幼态|幼齿)",
    re.IGNORECASE,
)
AGE_RISK = re.compile(r"(?:未成年|学生妹|学生辈|少女|幼女|小女孩|好嫩|很嫩|幼态|幼齿)")
SAFE_GENERAL = re.compile(
    r"(?:签到|积分|排行|发言|冒泡|有人吗|有人在吗|在吗|来了|早上好|晚上好|"
    r"天气|下雨|刮风|风大|几度|北京|天津|上海|重庆|山东|青岛|普通群友|自然|直接|友好|安全话题)"
)
SAFE_APPEARANCE = re.compile(
    r"(?:成年|成人|老师|穿搭|气质|身高|高挑|匀称|身材|曲线|显身材|样貌|长相|好看|漂亮|"
    r"腿又长|腿长|腿又白|腿白|皮肤白|高跟鞋|黑丝|丝袜|性感|撩人|状态很好|有活力)"
)
SAFE_GROUP = re.compile(r"(?:兴趣|交流|聊天|签到|天气|城市|生活|群友|普通)")


SYSTEM_PROMPT = """Generate Chinese community replies for a normal Telegram group using only the supplied sanitized input.
Every referenced person is an adult. You may continue an existing non-explicit adult topic about general appearance, figure, silhouette, long fair legs, stockings, high heels, sexy styling, alluring adult vibe, outfit, energy, or overall condition. Never introduce a new person or describe anyone as a student, minor, girl, tender-looking person, or with a youth analogy.
Do not facilitate or mention prices, payments, contacts, private messages, bookings, services, locations, transactions, intimate body parts, or sexual acts. Do not mention filtering, policy, risk, AI, prompts, or moderation.
For safe_context, reuse at least one explicit safe topic or phrase from the sanitized context. For generic_warmup, use only a greeting, check-in, weather, sign-in, or presence question. Do not invent experience, work, activities, repairs, locations, or facts.
Write casual natural Chinese, preferably 8 to 24 Chinese characters per draft. Output one JSON object only. No Markdown fences, thinking, prose, prefix, suffix, comments, or extra fields. Use exactly the supplied keys and enum values; context_source must match the input."""


@dataclass(frozen=True)
class GroupPromptBundle:
    system_prompt: str
    user_prompt: str
    context_source: str
    sanitized_context: tuple[str, ...]
    input_payload: dict[str, Any]
    output_contract: dict[str, Any]


def normalize(value: object) -> str:
    return SPACE.sub(" ", str(value or "")).strip()


def safe_clauses(value: object) -> list[str]:
    text = LINE_PREFIX.sub("", normalize(value))
    clauses = [normalize(item) for item in CLAUSE_SPLIT.split(text)]
    safe: list[str] = []
    for item in clauses:
        if not item or AGE_RISK.search(item):
            continue
        if not FORBIDDEN.search(item):
            safe.append(item)
            continue
        cleaned = normalize(FORBIDDEN.sub("", item))
        if cleaned and _allowed_clause(cleaned):
            safe.append(cleaned)
    return safe


def _allowed_clause(value: str) -> bool:
    return bool(SAFE_GENERAL.search(value) or SAFE_APPEARANCE.search(value))


def sanitize_group_messages(messages: list[str]) -> list[str]:
    safe: list[str] = []
    for message in messages:
        safe.extend(safe_clauses(message))
    return safe[-MAX_SAFE_MESSAGES:]


def _safe_group_label(value: object, group_id: object) -> str:
    label = normalize(value)
    if label and SAFE_GROUP.search(label) and not FORBIDDEN.search(label):
        return label
    return f"生产群-{int(group_id or 0)}"


def _safe_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, raw in value.items():
        clauses = safe_clauses(raw)
        if clauses:
            result[str(key)] = "；".join(clauses[:3])
    return result


def contains_disallowed_group_content(value: object) -> bool:
    return bool(FORBIDDEN.search(normalize(value)))


def _safe_target(value: object, label_key: str) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    label = safe_clauses(value.get(label_key))
    description = safe_clauses(value.get("description"))
    result = {label_key: label[0]} if label else {}
    if description:
        result["description"] = description[0]
    return result


def _safe_slots(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    allowed = ("sequence_index", "slot_id", "account_id", "act_type", "reply_to_sequence_index")
    return [{key: slot.get(key) for key in allowed if key in slot} for slot in value if isinstance(slot, dict)]


def _safe_reply_targets(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    targets: list[dict[str, str]] = []
    for item in value:
        clauses = safe_clauses((item or {}).get("preview") if isinstance(item, dict) else "")
        if clauses:
            targets.append({"preview": clauses[0]})
    return targets


def output_contract(context_source: str, count: int) -> dict[str, Any]:
    drafts = []
    for index in range(max(1, int(count or 1))):
        drafts.append({
            "sequence_index": index + 1,
            "reply_to_sequence_index": None,
            "persona": "普通群友",
            "content": "中文回复",
            "risk_level": "low",
            "intent": "check_in|follow_up|light_comment",
            "mood": "casual|curious|friendly",
            "material_intent": "",
            "allow_material": False,
        })
    return {"decision": "reply", "context_source": context_source, "drafts": drafts}


def build_group_prompt(
    config: dict,
    *,
    target_label: str,
    history: str,
    count: int,
    reply_targets: list[dict] | None = None,
) -> GroupPromptBundle:
    messages = sanitize_group_messages(str(history or "").splitlines())
    context_source = "safe_context" if messages else "generic_warmup"
    payload = {
        "group_label": _safe_group_label(target_label, config.get("target_group_id") or config.get("group_id")),
        "account_personas": _safe_map(config.get("account_personas")),
        "account_profiles": _safe_map(config.get("account_profiles")),
        "active_topic": _safe_target(config.get("active_topic_direction"), "title"),
        "active_teacher": _safe_target(config.get("active_teacher_target"), "name"),
        "generation_slots": _safe_slots(config.get("generation_slots")),
        "reply_targets": _safe_reply_targets(reply_targets),
        "context_source": context_source,
        "sanitized_context": messages,
    }
    contract = output_contract(context_source, count)
    user_prompt = (
        "Sanitized production-shaped input:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f"Generate exactly {max(1, int(count or 1))} Chinese draft(s). Return this exact JSON structure with placeholder values replaced:\n"
        f"{json.dumps(contract, ensure_ascii=False, indent=2)}"
    )
    return GroupPromptBundle(SYSTEM_PROMPT, user_prompt, context_source, tuple(messages), payload, contract)


__all__ = [
    "DRAFT_KEYS",
    "GroupPromptBundle",
    "build_group_prompt",
    "contains_disallowed_group_content",
    "sanitize_group_messages",
]
