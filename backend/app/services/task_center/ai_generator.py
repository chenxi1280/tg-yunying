from __future__ import annotations

import re
from dataclasses import replace
from difflib import SequenceMatcher

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.ai_gateway import DEFAULT_AI_REQUEST_TIMEOUT_SECONDS, normalize_ai_model_name
from app.models import AiProvider, AiProviderHealthStatus, PromptTemplate, TenantAiSetting
from app.services._common import ai_gateway
from app.services.ai_config import ai_provider_credentials
from app.services.content_filters import looks_like_generated_template_noise, looks_like_operator_ui_content


AI_GENERATION_UNAVAILABLE_MESSAGE = "AI 生成不可用，等待恢复后继续执行"
GROUP_CHAT_PURPOSE = "群活跃续聊"
CHANNEL_COMMENT_PURPOSE = "频道评论"
AI_CONTENT_REQUEST_TIMEOUT_SECONDS = 120
LONG_RUNNING_AI_PURPOSES = frozenset({GROUP_CHAT_PURPOSE, CHANNEL_COMMENT_PURPOSE})
SENSITIVE_CONTEXT_GUIDANCE = (
    "成人交易/性服务描述可以作为既有上下文理解和引用，但回复只能围绕原文已有事实做自然短评或追问；"
    "不要新增联系方式、价格、邀约或交易撮合信息，不要编造亲身交易经历。"
)
SENSITIVE_CONTEXT_SUMMARY = "成人服务描述已按安全口径概括：原文包含服务项目、价格或联系信息，生成时不得复述、扩写或撮合。"
SENSITIVE_CONTEXT_MARKERS = (
    "无套",
    "口活",
    "陪洗",
    "制服",
    "丝袜",
    "洛丽塔",
    "潮喷",
    "舌吻",
    "上课费用",
    "联系方",
    "嫩妹车",
    "颜值车",
    "态度车",
    "工兵",
    "出击老师",
)
AI_PROVIDER_REFUSAL_MARKERS = (
    "the request was rejected",
    "considered high risk",
    "content policy",
    "policy violation",
    "safety policy",
    "cannot comply",
    "can't comply",
    "i can't assist",
    "i cannot assist",
    "unable to comply",
    "请求被拒绝",
    "内容政策",
    "安全策略",
    "无法协助",
)


class AiGenerationUnavailable(RuntimeError):
    pass


def _provider(session: Session, tenant_id: int, provider_id: int | None = None, model_name: str = "") -> tuple[AiProvider | None, TenantAiSetting | None]:
    setting = session.scalar(select(TenantAiSetting).where(TenantAiSetting.tenant_id == tenant_id))
    if not setting or not setting.ai_enabled:
        return None, setting
    normalized_model = normalize_ai_model_name(model_name)
    if provider_id:
        provider = session.get(AiProvider, provider_id)
        if provider and provider.is_active and provider.health_status == AiProviderHealthStatus.HEALTHY.value:
            return provider, setting
    if normalized_model:
        provider = _provider_for_model(session, normalized_model)
        if provider:
            return provider, setting
    if setting.default_provider_id:
        provider = session.get(AiProvider, setting.default_provider_id)
        if provider and provider.is_active and provider.health_status == AiProviderHealthStatus.HEALTHY.value:
            return provider, setting
    provider = session.scalar(
        select(AiProvider)
        .where(AiProvider.is_active.is_(True), AiProvider.health_status == AiProviderHealthStatus.HEALTHY.value)
        .order_by(AiProvider.id.asc())
    )
    return provider, setting


def _provider_for_model(session: Session, model_name: str) -> AiProvider | None:
    family = _model_family(model_name)
    if not family:
        return None
    providers = session.scalars(
        select(AiProvider)
        .where(AiProvider.is_active.is_(True), AiProvider.health_status == AiProviderHealthStatus.HEALTHY.value)
        .order_by(AiProvider.id.asc())
    ).all()
    exact = next((provider for provider in providers if normalize_ai_model_name(provider.model_name) == model_name), None)
    if exact:
        return exact
    return next((provider for provider in providers if _model_family(provider.model_name) == family or _model_family(provider.provider_name) == family or _model_family(provider.base_url) == family), None)


def _model_family(value: str) -> str:
    normalized = value.lower()
    if "deepseek" in normalized:
        return "deepseek"
    if "mimo" in normalized or "xiaomimimo" in normalized:
        return "mimo"
    return ""


def generate_contents(
    session: Session,
    tenant_id: int,
    *,
    topic: str,
    requirements: str,
    provider_id: int | None = None,
    model_name: str = "",
    count: int,
    purpose: str,
    target_label: str = "",
    system_prompt: str | None = None,
) -> tuple[list[str], int]:
    provider, setting = _provider(session, tenant_id, provider_id, model_name)
    if not provider or not setting:
        if purpose in {GROUP_CHAT_PURPOSE, CHANNEL_COMMENT_PURPOSE}:
            raise AiGenerationUnavailable(f"{AI_GENERATION_UNAVAILABLE_MESSAGE}：{_unavailable_reason(setting)}")
        return _fallback_contents(topic, requirements, purpose, target_label, count), 0
    if purpose == GROUP_CHAT_PURPOSE:
        prompt = _group_chat_prompt(count, target_label, topic, requirements)
        persona_set = ["爱提问的群友", "补充细节的群友", "轻松接话的群友", "有经验的群友", "随口吐槽的群友"]
        tone = "像真实 Telegram 群成员聊天，短句、差异化、不要复读"
    elif purpose == CHANNEL_COMMENT_PURPOSE:
        prompt = _channel_comment_prompt(count, target_label, topic, requirements)
        persona_set = ["随手评论的读者", "追问细节的读者", "补充经验的读者", "轻松接话的读者"]
        tone = "像真实 Telegram 频道评论区，短句、贴原文、不重复"
    else:
        prompt = (
            f"请生成 {count} 条 Telegram {purpose}内容。\n"
            f"目标：{target_label}\n"
            f"主题：{topic}\n"
            f"要求：{requirements}\n"
            "每条都要自然、口语化、不要编号，不要暴露 AI 或运营任务。\n"
            '只输出 JSON：{"drafts":[{"persona":"自然用户","content":"内容","risk_level":"低"}]}'
        )
        persona_set = ["老用户", "新用户", "活跃成员", "路人"]
        tone = "自然、口语化、不同账号表达不重复"
    try:
        credentials = ai_provider_credentials(provider)
        if model_name.strip():
            credentials = replace(credentials, model_name=normalize_ai_model_name(model_name))
        result = ai_gateway.generate_drafts(
            credentials,
            prompt,
            count=count,
            topic=topic or requirements,
            tone=tone,
            persona_set=persona_set,
            temperature=max(float(setting.temperature or 0.7), 0.75) if purpose in {GROUP_CHAT_PURPOSE, CHANNEL_COMMENT_PURPOSE} else setting.temperature,
            max_tokens=max(setting.max_tokens, 1024),
            system_prompt=system_prompt,
            timeout=AI_CONTENT_REQUEST_TIMEOUT_SECONDS if purpose in LONG_RUNNING_AI_PURPOSES else DEFAULT_AI_REQUEST_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        if purpose in {GROUP_CHAT_PURPOSE, CHANNEL_COMMENT_PURPOSE}:
            raise AiGenerationUnavailable(f"{AI_GENERATION_UNAVAILABLE_MESSAGE}：{exc}") from exc
        raise
    contents = [candidate.content.strip() for candidate in result.candidates if candidate.content.strip()]
    if purpose == GROUP_CHAT_PURPOSE:
        contents = clean_group_chat_contents(contents)
        if not contents:
            raise AiGenerationUnavailable(AI_GENERATION_UNAVAILABLE_MESSAGE)
    if purpose == CHANNEL_COMMENT_PURPOSE:
        contents = clean_channel_comment_contents(contents, limit=count)
        if not contents:
            raise AiGenerationUnavailable("AI 评论候选质量不达标，未创建评论")
    usage = getattr(result, "usage", None)
    tokens = int(getattr(usage, "total_tokens", 0) or 0)
    if purpose in {GROUP_CHAT_PURPOSE, CHANNEL_COMMENT_PURPOSE}:
        return contents, tokens
    return contents[:count], tokens


def _group_chat_prompt(count: int, target_label: str, topic: str, requirements: str) -> str:
    return (
        f"请为 Telegram 群“{target_label}”生成 {count} 条多账号现场接话消息。\n"
        f"话题方向：{topic or '群聊日常活跃'}\n"
        f"上下文材料：\n{requirements or '暂无真人上下文'}\n\n"
        "先在心里判断当前群聊处在什么状态：有人刚提问、有人在吐槽、短暂停顿、还是完全冷场；"
        "然后让不同账号像真实群友一样接话，不要把任务拆成运营文案。\n\n"
        "截图里的真人聊天规律：大家不是在写完整观点，而是在短句接具体上下文；"
        "有真人上下文时只接上下文里已经出现的事实，没有上下文时只能低频暖场，不能编过去体验、位置、回访、准时、照片等细节。\n\n"
        "写法要求：\n"
        "1. 每条像手机上随手发的一句话，8-24 个字优先；可半句、可省主语、可只问一个小问题。\n"
        "2. 内容要落到真实群友会聊的细节，但细节必须来自上下文或账号记忆；没有锚点时只发轻微暖场或提问。\n"
        "3. 多账号之间要像同一群人在接话：第二个人可以承接刚才那句，第三个人补一个已出现细节，第四个人轻轻问一句。\n"
        "4. 少用书面连接词，少用完整因果；可以用“还真”“没得说”“这点我记住了”“下次试试”，但不要凭空说“我之前碰到”。\n"
        "5. 标点像群聊，不要像作文：多数短句不要句号，少用逗号/顿号/分号；需要停顿时优先用空格，问句可以保留问号。\n"
        "6. 不要复述或整段引用上下文；短词上下文要自然扩展成一个生活化小细节。\n"
        "7. 禁止使用这些模板句和近似句：看大家聊、刚看到大家提到、刚看到有人聊这个、顺着这个话题说、这个点挺有意思、这个点我也留意到了、可以继续聊聊、大家怎么看、有经验的朋友也可以补充下、我补充一下、这个话题、自然接一句、换个角度、轻量推进、具体场景、值得讨论。\n"
        "8. 不要连续使用“我觉得/感觉/确实/这个/大家”开头；不要使用 xx、X老师、某某 这类占位符；不要输出引号套引号；不要带编号、解释、括号备注。\n"
        "9. 黑话词表是理解口径，不是展示内容；该用行业口吻时自然用，不要解释词表。\n"
        f"10. {SENSITIVE_CONTEXT_GUIDANCE}\n"
        '只输出 JSON：{"drafts":[{"sequence_index":1,"reply_to_sequence_index":null,"persona":"不同群友人设","content":"群里要发送的一句话","risk_level":"低"}]}'
    )


def _channel_comment_prompt(count: int, target_label: str, topic: str, requirements: str) -> str:
    return (
        f"请为 Telegram 频道“{target_label}”生成 {count} 条评论区短评论。\n"
        f"评论方向：{topic or '按频道消息自然评论'}\n"
        f"上下文材料：\n{requirements}\n\n"
        "这些评论会直接发到频道讨论区，所以必须像真实读者看完后随手回的一句话。"
        "只能接频道消息里已经出现的事实、数字、物品、场景或问题；不确定时就问一个小问题，不要编亲身经历。\n\n"
        "写法要求：\n"
        "1. 每条 6-22 个字优先，像手机评论，不像总结、审核意见或运营文案。\n"
        "2. 至少抓住原文里的一个具体词或细节；不要只说“内容不错”“值得讨论”。\n"
        "3. 多条之间要从不同角度切入：尺寸、使用感、疑问、补充、轻微吐槽都可以，但不要同义复读。\n"
        "4. 少用完整句号和书面连接词；可以半句收尾，可以问具体小问题。\n"
        "5. 禁止使用这些模板句和近似句：这个内容挺有参考价值、先收藏一下、这个角度不错、值得再讨论、说得比较实在、后面可以继续展开、可以继续看看、学习了、支持一下、不错不错、感谢分享。\n"
        "6. 不要暴露 AI、平台、任务、提示词；不要编号、解释、括号备注、引号套引号。\n"
        f"7. {SENSITIVE_CONTEXT_GUIDANCE}\n"
        '只输出 JSON：{"drafts":[{"persona":"不同读者人设","content":"评论区要发送的一句话","risk_level":"低"}]}'
    )


def _unavailable_reason(setting: TenantAiSetting | None) -> str:
    if not setting:
        return "租户 AI 配置不存在"
    if not setting.ai_enabled:
        return "租户 AI 配置未启用"
    return "没有健康 AI 供应商"


def _fallback_contents(topic: str, requirements: str, purpose: str, target_label: str, count: int) -> list[str]:
    topic_text = _fallback_topic(topic, requirements, target_label)
    if purpose == "群活跃续聊":
        context_text = _fallback_recent_context(requirements)
        if context_text:
            templates = [
                f"刚才那句 {context_text[:24]} 我懂",
                f"{context_text[:28]} 这个细节挺关键",
                f"我也想问下 {context_text[:24]}",
                f"{context_text[:28]} 这块可以再说说",
            ]
        else:
            templates = [
                "这会儿有点安静啊",
                "有人在吗",
                "我先冒个泡",
                f"{topic_text[:24]} 今天有人聊吗",
            ]
    else:
        templates = [(requirements or topic or "这事可以再看看。").strip()]
    return [templates[index % len(templates)] for index in range(max(1, count))][:count]


def _fallback_topic(topic: str, requirements: str, target_label: str) -> str:
    for pattern in (r"请以“([^”]+)”为方向", r"请以\"([^\"]+)\"为方向"):
        match = re.search(pattern, requirements or "")
        if match:
            return match.group(1).strip()
    if topic and topic != "群聊日常活跃":
        return topic.strip()
    if target_label:
        return f"{target_label}里的日常交流"
    return "群里的日常交流"


def _fallback_recent_context(requirements: str) -> str:
    skip_prefixes = (
        "当前群暂无可用历史消息",
        "请以",
        "上一轮AI发言",
        "上一轮 AI 发言",
    )
    for line in reversed((requirements or "").splitlines()):
        text = line.strip()
        if ":" in text:
            label, text = text.split(":", 1)
            if label.strip().startswith(("上一轮AI发言", "上一轮 AI 发言")):
                continue
            text = text.strip()
        if not text or text.startswith(skip_prefixes):
            continue
        if "当前群暂无可用历史消息" in text or "不要提到系统、任务或 AI" in text:
            continue
        if text:
            return text[:80]
    return ""


def _sanitize_sensitive_context(text: str) -> str:
    raw = str(text or "")
    if not any(marker in raw for marker in SENSITIVE_CONTEXT_MARKERS):
        return raw
    lines = [_sanitize_sensitive_line(line) for line in raw.splitlines()]
    cleaned: list[str] = []
    summary_added = False
    for line in lines:
        if not line:
            continue
        if line == SENSITIVE_CONTEXT_SUMMARY:
            if summary_added:
                continue
            summary_added = True
        cleaned.append(line)
    return "\n".join(cleaned)


def _sanitize_sensitive_line(line: str) -> str:
    text = str(line or "").strip()
    if not text:
        return ""
    if not any(marker in text for marker in SENSITIVE_CONTEXT_MARKERS):
        return text.replace("老师编号", "对象编号").replace("妹子花名", "对象花名")
    safe_parts = _sensitive_safe_facts(text)
    if safe_parts:
        return "；".join(safe_parts + [SENSITIVE_CONTEXT_SUMMARY])
    return SENSITIVE_CONTEXT_SUMMARY


def _sensitive_safe_facts(text: str) -> list[str]:
    facts: list[str] = []
    for label in ("所在位置", "老师编号", "妹子花名"):
        match = re.search(rf"{label}[】：:：\s]*([^；;\n，,]+)", text)
        if not match:
            continue
        safe_label = label.replace("老师", "对象").replace("妹子", "对象")
        facts.append(f"{safe_label}：{match.group(1).strip()}")
    return facts


def clean_group_chat_contents(contents: list[str]) -> list[str]:
    accepted: list[str] = []
    starts: set[str] = set()
    for content in contents:
        cleaned = _clean_generated_content(content)
        if not cleaned or _looks_like_bad_group_chat_content(cleaned):
            continue
        normalized = _normalize_for_similarity(cleaned)
        if len(normalized) < 2:
            continue
        start_key = normalized[:8]
        if start_key in starts:
            continue
        if any(SequenceMatcher(None, normalized, _normalize_for_similarity(item)).ratio() >= 0.68 for item in accepted):
            continue
        starts.add(start_key)
        accepted.append(cleaned)
    return accepted


def clean_channel_comment_contents(contents: list[str], previous_contents: list[str] | None = None, *, limit: int | None = None) -> list[str]:
    accepted: list[str] = []
    previous = [_normalize_for_similarity(item) for item in previous_contents or []]
    clusters = {_channel_comment_cluster(item) for item in previous_contents or []}
    clusters.discard("")
    for content in contents:
        cleaned = _clean_generated_content(content)
        if not cleaned or _looks_like_bad_channel_comment(cleaned):
            continue
        normalized = _normalize_for_similarity(cleaned)
        if len(normalized) < 2:
            continue
        cluster = _channel_comment_cluster(cleaned)
        if cluster and cluster in clusters:
            continue
        if any(SequenceMatcher(None, normalized, item).ratio() >= 0.62 for item in previous):
            continue
        if any(SequenceMatcher(None, normalized, _normalize_for_similarity(item)).ratio() >= 0.68 for item in accepted):
            continue
        accepted.append(cleaned)
        if cluster:
            clusters.add(cluster)
        if limit and len(accepted) >= max(1, int(limit)):
            break
    return accepted


def _channel_comment_cluster(content: str) -> str:
    text = _normalize_for_similarity(content)
    clusters = [
        ("generic_reference", ("参考价值", "收藏一下", "值得讨论", "继续展开", "继续看看", "角度不错", "说得比较实在")),
        ("generic_support", ("支持一下", "不错不错", "感谢分享", "学习了")),
    ]
    for cluster, markers in clusters:
        if any(_normalize_for_similarity(marker) in text for marker in markers):
            return cluster
    return ""


def _looks_like_bad_channel_comment(content: str) -> bool:
    if _looks_like_ai_provider_refusal(content):
        return True
    if _channel_comment_cluster(content):
        return True
    markers = (
        "这个内容",
        "这个角度",
        "这个观点",
        "后面可以",
        "值得再",
        "可以继续",
        "先收藏",
        "有参考",
        "比较实在",
        "支持一下",
        "只输出 JSON",
        "risk_level",
        "persona",
    )
    if any(marker in content for marker in markers):
        return True
    return looks_like_generated_template_noise(content) or looks_like_operator_ui_content(content)


def _clean_generated_content(content: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(content or "")).strip()
    cleaned = re.sub(r"^(?:[-*\d.、\s]+)", "", cleaned).strip()
    return _humanize_group_chat_punctuation(cleaned)[:2000]


def _humanize_group_chat_punctuation(content: str) -> str:
    text = str(content or "").strip()
    if not text:
        return ""
    text = re.sub(r"[。．.]+$", "", text).strip()
    text = re.sub(r"[，、；：]+", " ", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff]),|,(?=[\u4e00-\u9fff])", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_for_similarity(content: str) -> str:
    return re.sub(r"[\s，。！？!?、,.；;：:\"'“”‘’（）()\[\]【】]+", "", content.lower())


def _looks_like_bad_group_chat_content(content: str) -> bool:
    if _looks_like_ai_provider_refusal(content):
        return True
    markers = (
        "当前群暂无可用历史消息",
        "不要提到系统",
        "不要提到系统、任务或 AI",
        "不要提到系统、任务或AI",
        "生成自然开场",
        "只输出 JSON",
        "risk_level",
        "persona",
        "[已撤回的内部提示词",
        "看大家聊",
        "刚看到大家提到",
        "刚看到有人聊这个",
        "顺着这个话题说",
        "这个点挺有意思",
        "这个点我也留意到了",
        "可以继续聊聊",
        "有经验的朋友也可以补充",
        "这个话题",
        "自然接一句",
        "换个角度",
        "轻量推进",
        "具体场景",
        "值得讨论",
    )
    if any(marker in content for marker in markers):
        return True
    if re.search(r"(?i)(?:\bxx\b|x老师|某某|某个)", content):
        return True
    if looks_like_generated_template_noise(content) or looks_like_operator_ui_content(content):
        return True
    return content.count("“") + content.count("”") >= 4


def _looks_like_ai_provider_refusal(content: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(content or "").strip().lower())
    return any(marker in normalized for marker in AI_PROVIDER_REFUSAL_MARKERS)


def generate_group_messages(session: Session, tenant_id: int, config: dict, *, count: int, target_label: str, history: str = "") -> tuple[list[str], int]:
    personas = config.get("account_personas") if isinstance(config.get("account_personas"), dict) else {}
    persona_prompt = ""
    if personas:
        persona_prompt = "账号角色设定：\n" + "\n".join(f"- 账号 {account_id}: {role}" for account_id, role in personas.items() if str(role).strip())
    memories = config.get("account_memories") if isinstance(config.get("account_memories"), dict) else {}
    memory_prompt = ""
    if memories:
        memory_prompt = "账号历史记忆：\n" + "\n".join(f"- 账号 {account_id}: {memory}" for account_id, memory in memories.items() if str(memory).strip())
    profiles = config.get("account_profiles") if isinstance(config.get("account_profiles"), dict) else {}
    profile_prompt = ""
    if profiles:
        profile_prompt = "账号长期画像：\n" + "\n".join(f"- 账号 {account_id}: {profile}" for account_id, profile in profiles.items() if str(profile).strip())
    topic_thread = str(config.get("topic_thread") or "").strip()
    topic_thread_prompt = f"话题脉络：\n{topic_thread}" if topic_thread else ""
    topic_plan = str(config.get("topic_plan") or "").strip()
    topic_plan_prompt = f"本轮话题计划：\n{topic_plan}" if topic_plan else ""
    slang_prompt = _slang_system_prompt(session, tenant_id, config)
    requirements = "\n".join(
        part
        for part in [
            config.get("topic_hint") or "",
            topic_thread_prompt,
            topic_plan_prompt,
            persona_prompt,
            memory_prompt,
            profile_prompt,
            history,
            config.get("system_prompt_override") or "",
        ]
        if part
    )
    requirements = _sanitize_sensitive_context(requirements)
    contents, tokens = generate_contents(
        session,
        tenant_id,
        topic=config.get("topic_hint") or "群聊日常活跃",
        requirements=requirements,
        provider_id=config.get("ai_provider_id"),
        model_name=str(config.get("ai_model") or ""),
        count=count,
        purpose="群活跃续聊",
        target_label=target_label,
        system_prompt=_group_chat_system_prompt(slang_prompt),
    )
    return _trim(contents, config.get("max_message_length")), tokens


def _group_chat_system_prompt(slang_prompt: str) -> str:
    base = (
        "你只负责把 Telegram 群友的临场接话包装成 JSON；不要写运营话术、公告、总结或解释。"
        "输出要像普通人在群里回消息：短、碎、具体；只有上下文出现过的事实才可以承接成经历或细节。"
        "没有真人上下文时只做低频暖场或提问，不要编上次经历、位置、回访、准点、照片等事实。"
        "不要每句都补完整逗号和句号，短句可以直接收尾；不要说“这个话题”“自然接一句”“换个角度”。"
        f"{SENSITIVE_CONTEXT_GUIDANCE}"
    )
    if not slang_prompt:
        return base
    return f"{base}\n\n{slang_prompt}"


def _channel_comment_system_prompt() -> str:
    return (
        "你只负责生成 Telegram 频道评论区的真实读者短评，并输出 JSON。"
        "评论必须贴频道原文里的具体信息，不写泛泛表扬、收藏、值得讨论、继续展开。"
        "如果原文细节不足，优先问一个具体小问题，不要编经历或使用模板。"
        f"{SENSITIVE_CONTEXT_GUIDANCE}"
    )


def _slang_system_prompt(session: Session, tenant_id: int, config: dict) -> str:
    parts = [
        _slang_prompt_template(session, tenant_id, config.get("slang_prompt_template_id")),
        _slang_terms_prompt(config.get("slang_terms")),
    ]
    return "\n\n".join(part for part in parts if part)


def _slang_prompt_template(session: Session, tenant_id: int, template_id: object) -> str:
    try:
        resolved_id = int(template_id or 0)
    except (TypeError, ValueError):
        raise AiGenerationUnavailable("AI 黑话配置不存在或已禁用")
    if not resolved_id:
        return ""
    template = session.scalar(
        select(PromptTemplate).where(
            PromptTemplate.id == resolved_id,
            PromptTemplate.is_active.is_(True),
            PromptTemplate.template_type == "AI黑话词表",
            or_(PromptTemplate.tenant_id == tenant_id, PromptTemplate.tenant_id.is_(None)),
        )
    )
    if not template or not template.content.strip():
        raise AiGenerationUnavailable("AI 黑话配置不存在或已禁用")
    return (
        f"AI 黑话配置：{template.name}\n"
        "以下内容是本任务的系统级行业口径，生成所有群聊消息时必须优先遵守；"
        "不要向群友解释这是配置或词表。\n"
        f"{template.content.strip()}"
    )


def _slang_terms_prompt(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    terms = [
        (str(source).strip(), str(target).strip())
        for source, target in value.items()
        if str(source).strip() and str(target).strip()
    ]
    if not terms:
        return ""
    lines = "\n".join(f"- {source} => {target}" for source, target in terms[:50])
    return (
        "行业黑话/俗语口径（强制遵守）：\n"
        f"{lines}\n"
        "生成时遇到左侧词或对应场景，不按字面含义解释，必须用右侧口径理解和表达；不要向群友解释这是词表。"
    )


def generate_channel_comments(session: Session, tenant_id: int, config: dict, *, count: int, message_content: str, target_label: str) -> tuple[list[str], int]:
    topic = config.get("topic_hint") or "频道评论"
    safe_message_content = _sanitize_sensitive_context(message_content)
    requirements = (
        f"频道消息：{safe_message_content}\n"
        f"评论风格：{config.get('comment_style') or 'mixed'}\n"
        f"语言：{config.get('language') or 'zh-CN'}\n"
        f"{_sanitize_sensitive_context(config.get('system_prompt_override') or '')}"
    )
    contents, tokens = generate_contents(
        session,
        tenant_id,
        topic=topic,
        requirements=requirements,
        provider_id=config.get("ai_provider_id"),
        model_name=str(config.get("ai_model") or ""),
        count=count,
        purpose="频道评论",
        target_label=target_label,
        system_prompt=_channel_comment_system_prompt(),
    )
    return _trim(contents, config.get("max_comment_length")), tokens


def rewrite_relay_content(session: Session, tenant_id: int, config: dict, content: str, *, target_label: str) -> tuple[str, int]:
    mode = config.get("content_mode") or "light_rewrite"
    if mode == "raw":
        return content, 0
    if mode == "light_rewrite":
        from app.services.campaign_runs import light_rewrite_message

        return light_rewrite_message(content), 0
    purpose = "群消息摘要" if mode == "summary" else "群消息改写"
    contents, tokens = generate_contents(
        session,
        tenant_id,
        topic=config.get("rewrite_prompt") or purpose,
        requirements=content,
        count=1,
        purpose=purpose,
        target_label=target_label,
    )
    return (contents[0] if contents else content), tokens


def _trim(contents: list[str], max_length: int | None) -> list[str]:
    if not max_length:
        return contents
    return [item[: int(max_length)] for item in contents]


__all__ = [
    "AI_GENERATION_UNAVAILABLE_MESSAGE",
    "AiGenerationUnavailable",
    "clean_channel_comment_contents",
    "clean_group_chat_contents",
    "generate_channel_comments",
    "generate_group_messages",
    "rewrite_relay_content",
]
