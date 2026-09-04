from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from .ai_context_information import meaningful_context_text
from app.services.automation_identity import with_automation_identity
from .ai_group_daily_theme import (
    DAILY_THEME_VERSION,
    DailyExpressionContext,
    get_daily_vocabulary_theme,
    get_vocabulary_theme,
)


MAX_SAFE_MESSAGES = 5
DRAFT_KEYS = {
    "slot_id",
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
LINE_PREFIX = re.compile(
    r"^(?!(?:所在位置|位置|地址|服务|服务项目)[:：])[^:：\n]{1,40}[:：]\s*"
)
SPACE = re.compile(r"\s+")
GROUP_URL_PATTERN_SOURCE = (
    r"(?:"
    r"(?:https?|ftp|tg)://[^\s，。！？、]+|"
    r"(?:t\.me|telegra\.ph)/[A-Za-z0-9_+/]+|"
    r"www\.[^\s，。！？、]+|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?:/[^\s，。！？、]*)?|"
    r"\b(?:[a-zA-Z0-9](?:[-a-zA-Z0-9]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z]{2,63}(?:/[^\s，。！？、]*)?"
    r")"
)
GROUP_URL_PATTERNS = re.compile(GROUP_URL_PATTERN_SOURCE, re.IGNORECASE)
CONTACT_PATTERN_SOURCE = (
    r"(?:"
    # 1. 微信及变体
    r"微信|微信号|加v|v同步|vx|weixin|企微|加我v|留v|加威|威信|威同|微同|加微|留微|v号|V号|"
    # 2. 手机/电话类（国内、国际、带符号、电话前缀）
    r"\+\d{1,3}[- ]?\(?\d{2,4}\)?(?:[- ]?\d{3,4}){2}\b|"
    r"\(\d{2,4}\)[- ]?\d{3,4}[- ]?\d{3,4}\b|"
    r"\b\d{1,3}[- ]\d{3,4}[- ]\d{4}\b|"
    r"\b(?:\+?86[- ]?)?1[3-9]\d{9}\b|"
    r"\b1[3-9]\d[- ]?\d{4}[- ]?\d{4}\b|"
    r"(?:电话|手机|号码|致电|拨打|tel|phone|call)[：:\s]*\+?[\d() -]{7,20}|"
    # 3. QQ类（要求账号、号码或联系语境，避免把普通“企鹅”误判）
    r"(?:加|留(?:个)?|联系|给我|发我|找我|走)\s*(?:qq|企鹅|扣扣)(?:号|联系|私聊|聊|加我|发你|发我)?|"
    r"(?:qq|企鹅|扣扣)(?:号|联系|私聊|聊|加我|发你|发我|[：:\s]*[1-9]\d{4,10})|"
    r"\bqq\b|"
    # 4. TG/飞机/电报/私聊/用户名
    r"(?:飞机|电报|纸飞机|飞机号|电报号|tg号|telegram|tele|加飞机|加电报|走飞机|走电报|走tg)[：:\s]*[A-Za-z0-9_]{3,}|"
    r"(?:飞机号|电报号|加飞机|加电报|走飞机|走电报|纸飞机|飞机联系|电报联系|telegram|tg号|\btg\b)|"
    r"@[A-Za-z0-9_]{3,}|"
    r"私聊|私信|加我|联系方式|联系电话|留联系方式|留个联系方式|怎么联系|联系我|"
    # 5. 链接/协议/短链/裸域名
    rf"{GROUP_URL_PATTERN_SOURCE}"
    r")"
)
CONTACT_PATTERNS = re.compile(CONTACT_PATTERN_SOURCE, re.IGNORECASE)
FORBIDDEN = re.compile(
    rf"(?:{CONTACT_PATTERN_SOURCE}|"
    # 交易与价格词
    r"多少钱|多少米|价格|价位|收费|费用|预算|套餐|付款|支付|转账|红包|扫码|"
    # 露骨生理词与违规交易词
    r"做爱|性交|大蟒蛇|全套|半套|口活|无套口|招嫖|嫖客|包养|"
    # 未成年风险
    r"未成年|学生妹|学生辈|少女|幼女|小女孩|好嫩|很嫩|幼态|幼齿"
    r")",
    re.IGNORECASE,
)
GENERAL_CONTEXT_STRICT = re.compile(
    r"(?:地址|位置|定位|酒店|宾馆|房间|开房|上门|到店|预约|预订|安排|档期|可约|"
    r"资源|求推荐|有没有好|上牌|上车|色情|寻欢客|妹妹|小妹|技师|楼凤|外围|"
    r"面付|抓龙筋|可外|可包时)",
    re.IGNORECASE,
)
GENERAL_CONTEXT_SOFT = re.compile(r"(?:服务)", re.IGNORECASE)
AGE_RISK = re.compile(r"(?:未成年|学生妹|学生辈|少女|幼女|小女孩|好嫩|很嫩|幼态|幼齿)")
BOT_JUNK_PATTERNS = re.compile(
    r"(?:关注.*频道|需要关注|邀请成功|获得.*积分|当前总共邀请|抽红包|抽奖|点我头像|加v|v同步|"
    r"无限畅饮|摸摸唱|女仆92|挖宝|集齐碎片|今日发言量|群内排名|由于系统原因|"
    r"还没有你的定位|为了保护隐私|更新后回到本群发送|查询附近老师|点击底部按钮)",
    re.IGNORECASE,
)
SAFE_GENERAL = re.compile(
    r"(?:有人吗|有人在吗|在吗|来了|附近|夜宵|吃饭|电影|手机|通勤|音乐|游戏|"
    r"北京|天津|上海|重庆|山东|青岛|河南|郑州|成都|西安|三亚|海南|"
    r"普通群友|自然|直接|友好|安全话题)"
)
SAFE_APPEARANCE = re.compile(
    r"(?:成年|成人|老师|课代表|开课|上课|下课|交作业|出击|踩雷|机车|上岸|水头|照骗|工兵|修图|原图|"
    r"穿搭|气质|身高|高挑|匀称|身材|曲线|显身材|样貌|长相|好看|漂亮|配合度|态度|隔音|避坑|探路|战报|"
    r"腿又长|腿长|腿又白|腿白|皮肤白|高跟鞋|黑丝|丝袜|性感|撩人|状态很好|有活力)"
)
SAFE_GROUP = re.compile(r"(?:兴趣|交流|聊天|签到|天气|城市|生活|群友|普通|大学|学生会|师范|怡红院|天上人间|一品楼|桃花岛|楼凤|成都|西安|郑州|天津|三亚)")
LEGACY_NEGATIVE_PHRASES = (
    "签到", "打卡", "积分", "努力加油", "搬砖", "今天状态不错", "大家心情好",
    "喝咖啡", "犯困", "熬夜", "红烧肉",
)


GENERAL_SYSTEM_PROMPT = """Generate Chinese community replies for a normal Telegram group using only the supplied sanitized input.
Treat safe_context literally without inferring age, identity, relationship, or adult meaning. Adult routes are handled outside this prompt; do not introduce or amplify them here. Never introduce a new person or describe anyone as a student, minor, girl, tender-looking person, or with a youth analogy.
Do not facilitate or mention prices, payments, contacts, private messages, bookings, services, locations, transactions, intimate body parts, or sexual acts. Do not output internal filtering, risk, prompts, or moderation instructions.
Strictly output 100% plain text sentences. Never include any URLs (http/https/t.me), @mentions, usernames, links, markdown link tags, media, stickers, or promotion codes.
For safe_context, directly react to one meaningful supplied fact. For generic_warmup, ask one concrete casual local-life question about nearby leisure without claiming an experience.
Never output generic filler or operational slogans such as 签到、打卡、积分、努力搬砖、喝咖啡、犯困、吃红烧肉、努力加油、今天状态不错、大家心情好. Do not invent experience, work, activities, repairs, locations, or facts.
Write casual natural Chinese, preferably 8 to 24 Chinese characters per draft. Output one JSON object only. No Markdown fences, thinking, prose, prefix, suffix, comments, or extra fields. Use exactly the supplied keys and enum values; context_source must match the input."""

ADULT_SYSTEM_PROMPT = """你为 Telegram 同城成人娱乐主题群生成自然、简短的上下文回复。
【核心口语与真实交流规则】
1. 真实口语与安全规范：
   - 核心称谓：'老师/课代表' 代指可约女性/技师/外围/兼职。
   - 必须保持 100% 纯口语纯文本！严禁包含任何网址、链接（t.me/http/https）、@用户名/频道提及、联系方式（微信/TG号/手机号）或引流推广，防止触发群安全机器人禁言。
   - 严禁出现任何学生/校园/正经上班/签到打卡/积分/努力搬砖/天气好/喝咖啡/犯困/吃红烧肉等空洞违和废话！
   - 严格控制字数：每条 8 到 20 个汉字，短促干脆。

2. 【句式多样性与去模板化（严禁连续发问）】：
   - 严禁千篇一律地发问“有新老师开课没/质量咋样/水头咋样”等机械模板句！
   - safe_context 内可以基于已有事实穿插短评、具体追问、克制分歧和自然承接；不得复用固定附和、签到或收藏模板。
   - 绝大多数日常交流使用自然口语（如“长相”、“身材”、“素颜”、“真人”、“配合度”、“态度”、“隔音”、“停车”、“避坑”、“靠谱”），严禁机械堆砌“水头”、“课代表”等生硬暗语。

3. 【事实锚点铁律（严禁无依据编造经历与地点）】：
   - 严禁在没有群聊上下文证据时凭空捏造个人到店经历、出击体验或虚构具体商圈公寓地点！
   - 对于 safe_context，紧扣群聊上下文已提及的事实/老师顺着接话（如附和、吐槽照骗、询问细节）；
   - 对于 generic_warmup / idle 场景，只能提出不指向具体人物、资源、地点或服务的开放问题；禁止附和、求推荐或声称个人经历！
   - 平时闲聊不主动提具体地名/路名；少用标点符号，多用空格断句，偶尔可带 1 个真实常用 emoji（如 😂/👍/🔥/🤦‍♂️/👀/🤔）。
Output one JSON object only. No Markdown fences, thinking, prose, prefix, suffix, comments, or extra fields. Use exactly the supplied keys and enum values; context_source must match the input."""

SYSTEM_PROMPT = GENERAL_SYSTEM_PROMPT

ADULT_CONTENT_ROUTES = frozenset({
    "adult_visual",
    "adult_product",
    "adult_service_inquiry",
    "adult_service_sensory",
    "adult_service",
})
ADULT_PERSONAS = ("探路工兵", "挑剔老炮", "随性吃瓜", "本地地胆")

def _configured_content_route(config: dict) -> str:
    contract = dict(config.get("_ai_content_contract") or {})
    return str(contract.get("content_route") or config.get("content_route") or "").strip()


def is_adult_content_config(config: dict | None) -> bool:
    config = config or {}
    if config.get("adult_prompt_enabled") is True:
        return True
    route = _configured_content_route(config)
    if route not in ADULT_CONTENT_ROUTES:
        return False
    if not config.get("ai_content_route_v2_enabled"):
        return True
    allowed = config.get("ai_content_allowed_routes") or config.get("allowed_routes") or ()
    return route in {str(item) for item in allowed}


def _is_adult_group_prompt(config: dict, target_label: str = "", context_text: str = "") -> bool:
    del target_label, context_text
    return is_adult_content_config(config)


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


def safe_clauses(value: object, *, allow_adult_context: bool = False) -> list[str]:
    text = LINE_PREFIX.sub("", normalize(value))
    clauses = [normalize(item) for item in CLAUSE_SPLIT.split(text)]
    safe: list[str] = []
    for item in clauses:
        if not item or AGE_RISK.search(item):
            continue
        if not allow_adult_context and GENERAL_CONTEXT_STRICT.search(item):
            continue
        has_forbidden = FORBIDDEN.search(item)
        has_general_soft = not allow_adult_context and GENERAL_CONTEXT_SOFT.search(item)
        if not has_forbidden and not has_general_soft:
            safe.append(item)
            continue
        cleaned = normalize(FORBIDDEN.sub("", GENERAL_CONTEXT_SOFT.sub("", item)))
        if cleaned and _allowed_clause(cleaned):
            safe.append(cleaned)
    return safe


def _allowed_clause(value: str) -> bool:
    return bool(SAFE_GENERAL.search(value) or SAFE_APPEARANCE.search(value))


def sanitize_group_messages(messages: list[str], *, allow_adult_context: bool = False) -> list[str]:
    safe: list[str] = []
    for message in messages:
        if BOT_JUNK_PATTERNS.search(str(message or "")):
            continue
        safe.extend(
            clause
            for clause in safe_clauses(message, allow_adult_context=allow_adult_context)
            if meaningful_context_text(clause)
        )
    return safe[-MAX_SAFE_MESSAGES:]


def _safe_group_label(value: object, group_id: object, *, allow_adult_context: bool = False) -> str:
    label = normalize(value)
    general_safe = allow_adult_context or bool(SAFE_GROUP.search(label))
    general_forbidden = not allow_adult_context and GENERAL_CONTEXT_STRICT.search(label)
    if label and general_safe and not general_forbidden and not FORBIDDEN.search(label) and not AGE_RISK.search(label):
        return label
    return f"生产群-{int(group_id or 0)}"


def _safe_map(value: object, *, allow_adult_context: bool = False) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, raw in value.items():
        clauses = safe_clauses(raw, allow_adult_context=allow_adult_context)
        if clauses:
            result[str(key)] = "；".join(clauses[:3])
    return result


def contains_disallowed_group_content(value: object, *, allow_adult_context: bool = False) -> bool:
    normalized = normalize(value)
    if FORBIDDEN.search(normalized):
        return True
    return not allow_adult_context and bool(
        GENERAL_CONTEXT_STRICT.search(normalized) or GENERAL_CONTEXT_SOFT.search(normalized)
    )


def _safe_target(value: object, label_key: str, *, allow_adult_context: bool = False) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    label = safe_clauses(value.get(label_key), allow_adult_context=allow_adult_context)
    description = safe_clauses(value.get("description"), allow_adult_context=allow_adult_context)
    result = {label_key: label[0]} if label else {}
    if description:
        result["description"] = description[0]
    return result


def _safe_slots(value: object, *, allow_adult_context: bool = False) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        _safe_slot(slot, allow_adult_context=allow_adult_context)
        for slot in value
        if isinstance(slot, dict)
    ]


def _safe_slot(slot: dict[str, Any], *, allow_adult_context: bool = False) -> dict[str, Any]:
    exact = (
        "sequence_index",
        "slot_id",
        "account_id",
        "act_type",
        "reply_to_sequence_index",
        "normal_text_ordinal",
        "topic_mode",
    )
    result = {key: slot.get(key) for key in exact if key in slot}
    for key in ("persona", "account_profile", "material_intent", "content_guidance"):
        clauses = safe_clauses(slot.get(key), allow_adult_context=allow_adult_context)
        if clauses:
            result[key] = "；".join(clauses[:3])
    topic = _safe_target(slot.get("topic_direction"), "title", allow_adult_context=allow_adult_context)
    teacher = _safe_target(slot.get("teacher_target"), "name", allow_adult_context=allow_adult_context)
    if topic:
        result["topic_direction"] = topic
    if teacher:
        result["teacher_target"] = teacher
    surfaces = safe_clauses(
        "；".join(str(item) for item in slot.get("vocabulary_surface_terms") or []),
        allow_adult_context=allow_adult_context,
    )
    if surfaces:
        result["optional_vocabulary_hints"] = surfaces[:2]
    return result


def _assign_adult_personas(
    slots: list[dict[str, Any]],
    account_personas: dict[str, str],
) -> list[dict[str, Any]]:
    assigned = []
    for index, slot in enumerate(slots):
        account_persona = account_personas.get(str(slot.get("account_id") or ""))
        persona = str(slot.get("persona") or account_persona or "").strip()
        if not persona:
            raw_position = slot.get("account_id") or slot.get("sequence_index") or index + 1
            try:
                position = int(raw_position) - 1
            except (TypeError, ValueError):
                position = index
            persona = ADULT_PERSONAS[position % len(ADULT_PERSONAS)]
        assigned.append({**slot, "persona": persona})
    return assigned


def _safe_reply_targets(value: object, *, allow_adult_context: bool = False) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    targets: list[dict[str, str]] = []
    for item in value:
        clauses = safe_clauses(
            (item or {}).get("preview") if isinstance(item, dict) else "",
            allow_adult_context=allow_adult_context,
        )
        if clauses:
            targets.append({"preview": clauses[0]})
    return targets


def output_contract(
    context_source: str,
    count: int,
    generation_slots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    slots = list(generation_slots or [])
    drafts = []
    for index in range(max(1, int(count or 1))):
        slot = slots[index] if index < len(slots) else {}
        drafts.append({
            "slot_id": str(slot.get("slot_id") or ""),
            "sequence_index": index + 1,
            "reply_to_sequence_index": slot.get("reply_to_sequence_index"),
            "persona": str(slot.get("persona") or "普通群友"),
            "content": "中文回复",
            "risk_level": "low",
            "intent": "topic_question|follow_up|light_comment",
            "mood": "casual|curious|friendly",
            "material_intent": "",
            "allow_material": False,
        })
    return {"decision": "reply", "context_source": context_source, "drafts": drafts}


def _prompt_payload(
    config: dict,
    *,
    target_label: str,
    messages: list[str],
    reply_targets: list[dict] | None,
    adult_context: bool,
) -> dict[str, Any]:
    active_topic = config.get("active_topic_direction")
    allow = adult_context
    account_personas = _safe_map(config.get("account_personas"), allow_adult_context=allow)
    generation_slots = _safe_slots(config.get("generation_slots"), allow_adult_context=allow)
    if any(slot.get("topic_mode") for slot in generation_slots):
        active_topic = None
    if adult_context:
        generation_slots = _assign_adult_personas(generation_slots, account_personas)
    return {
        "group_label": _safe_group_label(
            target_label,
            config.get("target_group_id") or config.get("group_id"),
            allow_adult_context=allow,
        ),
        "account_personas": account_personas,
        "account_memories": _safe_map(config.get("account_memories"), allow_adult_context=allow),
        "account_profiles": _safe_map(config.get("account_profiles"), allow_adult_context=allow),
        "active_topic": _safe_target(active_topic, "title", allow_adult_context=allow),
        "active_teacher": _safe_target(
            config.get("active_teacher_target"),
            "name",
            allow_adult_context=allow,
        ),
        "generation_slots": generation_slots,
        "reply_targets": _safe_reply_targets(reply_targets, allow_adult_context=allow),
        "context_source": "safe_context" if messages else "generic_warmup",
        "sanitized_context": messages,
    }


def get_adult_system_prompt(
    surface_scope_key: str | None = None,
    task_day: date | None = None,
) -> str:
    if not surface_scope_key:
        return ADULT_SYSTEM_PROMPT
    if task_day is None:
        raise ValueError("daily_theme_task_day_required")
    theme = get_daily_vocabulary_theme(surface_scope_key, task_day)
    return _with_daily_theme(ADULT_SYSTEM_PROMPT, theme.theme_id)


def _with_daily_theme(base: str, theme_id: int) -> str:
    theme = get_vocabulary_theme(theme_id)
    theme_section = f"\n\n【今日表达调色板：{theme.name}】\n- 调色板指引：{theme.tone_guidance}\n- 禁忌事项：{theme.prohibited_items}\n- 注：调色板仅调整表达方式，不改变任务话题、事实锚点与安全红线。"
    return base + theme_section


def _frozen_theme(config: dict, context: DailyExpressionContext | None) -> int | None:
    if context is not None:
        expected = get_daily_vocabulary_theme(context.surface_scope_key, context.task_day)
        if expected.theme_id != context.vocabulary_theme_id:
            raise ValueError("daily_theme_contract_invalid")
        return context.vocabulary_theme_id
    slots = [slot for slot in config.get("generation_slots") or [] if isinstance(slot, dict)]
    if not slots:
        return None
    first = slots[0]
    if not first.get("allocation_plan_id"):
        return None
    if first.get("daily_vocabulary_theme_version") != DAILY_THEME_VERSION:
        raise ValueError("daily_theme_contract_invalid")
    theme_id = int(first.get("daily_vocabulary_theme_id", -1))
    if any(int(slot.get("daily_vocabulary_theme_id", -1)) != theme_id for slot in slots):
        raise ValueError("daily_theme_contract_invalid")
    return get_vocabulary_theme(theme_id).theme_id


def build_group_prompt(
    config: dict,
    *,
    target_label: str,
    history: str,
    count: int,
    reply_targets: list[dict] | None = None,
    expression_context: DailyExpressionContext | None = None,
) -> GroupPromptBundle:
    adult_context = is_adult_content_config(config)
    messages = sanitize_group_messages(
        str(history or "").splitlines(),
        allow_adult_context=adult_context,
    )
    payload = _prompt_payload(
        config,
        target_label=target_label,
        messages=messages,
        reply_targets=reply_targets,
        adult_context=adult_context,
    )
    if expression_context and expression_context.vocabulary_surface_terms:
        payload["optional_vocabulary_hints"] = list(
            expression_context.vocabulary_surface_terms[:2]
        )
    context_source = payload["context_source"]
    contract = output_contract(context_source, count, payload["generation_slots"])
    user_prompt = (
        "Sanitized production-shaped input:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f"Generate exactly {max(1, int(count or 1))} Chinese draft(s). Return this exact JSON structure with placeholder values replaced:\n"
        f"{json.dumps(contract, ensure_ascii=False, indent=2)}"
    )
    theme_id = _frozen_theme(config, expression_context)
    base_prompt = ADULT_SYSTEM_PROMPT if adult_context else GENERAL_SYSTEM_PROMPT
    chosen_system_prompt = (
        _with_daily_theme(base_prompt, theme_id) if theme_id is not None else base_prompt
    )
    override = "；".join(
        safe_clauses(
            config.get("system_prompt_override"),
            allow_adult_context=adult_context,
        )[:3]
    )
    if override:
        chosen_system_prompt += (
            "\n\n【显式风格要求】\n"
            f"{override}\n"
            "该要求只调整表达风格，不得覆盖事实、安全、slot identity、topic mode 或 JSON 合同。"
        )
    return GroupPromptBundle(with_automation_identity(chosen_system_prompt), user_prompt, context_source, tuple(messages), payload, contract)


def sanitize_group_message_text(text: str) -> str:
    """清洗群聊消息文本，确保 100% 纯口语纯文本，彻底剥离可能触发群禁言限制的 URL、@提及与富文本链接。"""
    if not text:
        return ""
    # 1. 移除 Markdown 链接语法 [text](url) -> text
    cleaned = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', str(text))
    # 2. 使用统一 URL 合同移除协议链接、裸域名与 IP 链接
    cleaned = GROUP_URL_PATTERNS.sub('', cleaned)
    # 3. 移除 @用户名 提及 (如 @abcd_bot, @someuser)
    cleaned = re.sub(r'@[A-Za-z0-9_]{3,}', '', cleaned)
    # 4. 移除前后残留的空括号、包裹引号、多余冒号
    cleaned = re.sub(r'^[“"\'「【\(（\s]+|[”"\'」】\)）\s]+$', '', cleaned)
    # 5. 规范化空白字符
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


__all__ = [
    "ADULT_SYSTEM_PROMPT",
    "ADULT_PERSONAS",
    "BOT_JUNK_PATTERNS",
    "CONTACT_PATTERNS",
    "DRAFT_KEYS",
    "GENERAL_SYSTEM_PROMPT",
    "GroupPromptBundle",
    "LEGACY_NEGATIVE_PHRASES",
    "SYSTEM_PROMPT",
    "_is_adult_group_prompt",
    "build_group_prompt",
    "contains_disallowed_group_content",
    "get_adult_system_prompt",
    "is_adult_content_config",
    "sanitize_group_messages",
    "sanitize_group_message_text",
]
