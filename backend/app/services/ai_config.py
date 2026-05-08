from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ai_gateway import AiProviderCredentials, AiUsage, normalize_ai_model_name
from app.auth import CurrentUser
from app.models import (
    AiProvider,
    AiProviderHealthStatus,
    AiUsageLedger,
    AppUser,
    Campaign,
    ContentKeywordRule,
    Material,
    PromptTemplate,
    SchedulingSetting,
    Tenant,
    TenantAiSetting,
    TgGroup,
    UserTokenLedger,
)
from app.schemas import (
    AiProviderCreate,
    AiProviderUpdate,
    MaterialCreate,
    MaterialUpdate,
    ContentKeywordRuleCreate,
    ContentKeywordRuleUpdate,
    PromptTemplateCreate,
    PromptTemplateUpdate,
    SchedulingSettingUpdate,
    TenantAiSettingUpdate,
)
from app.security import decrypt_secret, encrypt_secret

from ._common import _now, ai_gateway, audit, require_tenant


def seed_ai_configuration(session: Session) -> None:
    default_templates = [
        PromptTemplate(
            tenant_id=None,
            template_type="系统决策提示词",
            name="默认系统决策提示词",
            content=(
                "你是 Telegram 运营平台的系统决策提示词。根据群配置、任务目标、素材和客户 AI 设置，"
                "决定本次是否调用 AI，以及使用哪类业务提示词：群活跃草稿、多账号对话脚本、素材配文或风险检查。"
                "若客户关闭 AI、任务目标不需要生成、或没有健康模型，应跳过 AI 并使用模板回退。"
            ),
        ),
        PromptTemplate(
            tenant_id=None,
            template_type="群活跃草稿",
            name="默认群活跃草稿",
            content=(
                "请为 Telegram 群生成 {{count}} 条自然聊天草稿，输出 JSON："
                "{\"drafts\":[{\"persona\":\"人设\",\"content\":\"消息内容\",\"risk_level\":\"低|中|高\",\"material_id\":null}]}。\n"
                "群名称：{{group_title}}\n话题方向：{{topic}}\n群运营方向：{{topic_direction}}\n语气：{{tone}}\n"
                "人设集合：{{persona_set}}\n禁用词：{{banned_words}}\n可用素材：{{materials}}\n"
                "要求：像真实群成员，不刷屏，不承诺收益，不输出解释。"
            ),
        ),
        PromptTemplate(
            tenant_id=None,
            template_type="多账号对话脚本",
            name="默认多账号对话脚本",
            content=(
                "请围绕 {{topic}} 为 Telegram 群生成 {{count}} 条多账号对话脚本，"
                "每条使用不同 persona，语气 {{tone}}，输出 JSON drafts。群：{{group_title}}，禁用词：{{banned_words}}。"
            ),
        ),
        PromptTemplate(
            tenant_id=None,
            template_type="监听上下文续聊脚本",
            name="默认监听上下文续聊脚本",
            content=(
                "你正在根据 Telegram 群的真人上下文生成自然续聊。请围绕 {{topic}} 生成 {{count}} 条，"
                "必须输出 json drafts。所选账号：{{selected_accounts}}。监听账号：{{listener_account}}。"
                "真人上下文：{{conversation_context}}。要求从 A 账号开始按顺序轮流接话，"
                "每条包含 sequence_index、persona、content、risk_level、suggested_account_id。"
            ),
        ),
        PromptTemplate(
            tenant_id=None,
            template_type="素材配文",
            name="默认素材配文",
            content="围绕 {{topic}} 给素材 {{materials}} 写 Telegram 群内自然配文，输出 JSON drafts。",
        ),
        PromptTemplate(
            tenant_id=None,
            template_type="风险检查",
            name="默认风险检查提示词",
            content="检查 Telegram 群消息是否包含禁用词、违规承诺、诱导链接或刷屏风险，输出风险等级和原因。",
        ),
    ]
    for default_template in default_templates:
        exists = session.scalar(
            select(PromptTemplate.id).where(
                PromptTemplate.tenant_id.is_(None),
                PromptTemplate.template_type == default_template.template_type,
            )
        )
        if not exists:
            session.add(default_template)
    session.flush()
    tenant_ids = list(session.scalars(select(Tenant.id)))
    default_provider_id = session.scalar(select(AiProvider.id).order_by(AiProvider.id.asc()))
    for tenant_id in tenant_ids:
        if not session.scalar(select(TenantAiSetting).where(TenantAiSetting.tenant_id == tenant_id)):
            session.add(
                TenantAiSetting(
                    tenant_id=tenant_id,
                    default_provider_id=default_provider_id,
                    ai_enabled=bool(default_provider_id),
                    fallback_to_mock=False,
                )
            )
    if not session.scalar(select(SchedulingSetting).where(SchedulingSetting.tenant_id.is_(None))):
        session.add(SchedulingSetting(tenant_id=None, jitter_min_seconds=15, jitter_max_seconds=180, batch_interval_seconds=45, respect_send_window=True))
    for tenant_id in tenant_ids:
        if not session.scalar(select(SchedulingSetting).where(SchedulingSetting.tenant_id == tenant_id)):
            session.add(SchedulingSetting(tenant_id=tenant_id, jitter_min_seconds=15, jitter_max_seconds=180, batch_interval_seconds=45, respect_send_window=True))
    session.flush()


def ai_provider_credentials(provider: AiProvider) -> AiProviderCredentials:
    if not provider.is_active:
        raise ValueError("AI provider disabled")
    if provider.health_status != AiProviderHealthStatus.HEALTHY.value:
        raise ValueError(f"AI provider unhealthy: {provider.health_status}")
    api_key = decrypt_secret(provider.api_key_ciphertext)
    if not api_key:
        raise ValueError("AI provider api key is empty")
    return AiProviderCredentials(
        provider_name=provider.provider_name,
        provider_type=provider.provider_type,
        base_url=provider.base_url,
        model_name=provider.model_name,
        api_key=api_key,
        api_key_header=provider.api_key_header,
    )


def list_ai_providers(session: Session) -> list[AiProvider]:
    return list(session.scalars(select(AiProvider).order_by(AiProvider.id.asc())))


def create_ai_provider(session: Session, payload: AiProviderCreate, actor: str) -> AiProvider:
    provider = AiProvider(
        provider_name=payload.provider_name,
        provider_type=payload.provider_type,
        base_url=payload.base_url,
        model_name=normalize_ai_model_name(payload.model_name),
        api_key_ciphertext=encrypt_secret(payload.api_key),
        api_key_header=payload.api_key_header,
        input_price_per_1k=payload.input_price_per_1k,
        output_price_per_1k=payload.output_price_per_1k,
        currency=payload.currency,
        is_billable=payload.is_billable,
        is_active=payload.is_active,
        health_status=AiProviderHealthStatus.HEALTHY.value if payload.is_active else AiProviderHealthStatus.DISABLED.value,
        notes=payload.notes,
    )
    session.add(provider)
    session.flush()
    audit(session, tenant_id=None, actor=actor, action="新增AI供应商", target_type="ai_provider", target_id=str(provider.id))
    session.commit()
    session.refresh(provider)
    return provider


def update_ai_provider(session: Session, provider_id: int, payload: AiProviderUpdate, actor: str) -> AiProvider:
    provider = session.get(AiProvider, provider_id)
    if not provider:
        raise ValueError("ai provider not found")
    data = payload.model_dump(exclude_unset=True)
    if data.get("model_name") is not None:
        data["model_name"] = normalize_ai_model_name(data["model_name"])
    for field in ["provider_name", "provider_type", "base_url", "model_name", "api_key_header", "notes", "currency"]:
        if data.get(field) is not None:
            setattr(provider, field, data[field])
    for field in ["input_price_per_1k", "output_price_per_1k", "is_billable"]:
        if field in data and data[field] is not None:
            setattr(provider, field, data[field])
    if data.get("api_key"):
        provider.api_key_ciphertext = encrypt_secret(data["api_key"])
    if data.get("is_active") is not None:
        provider.is_active = data["is_active"]
        provider.health_status = AiProviderHealthStatus.HEALTHY.value if provider.is_active else AiProviderHealthStatus.DISABLED.value
    provider.updated_at = _now()
    audit(session, tenant_id=None, actor=actor, action="更新AI供应商", target_type="ai_provider", target_id=str(provider.id))
    session.commit()
    session.refresh(provider)
    return provider


def check_ai_provider(session: Session, provider_id: int, actor: str) -> AiProvider:
    provider = session.get(AiProvider, provider_id)
    if not provider:
        raise ValueError("ai provider not found")
    provider.last_check_at = _now()
    if not provider.is_active:
        provider.health_status = AiProviderHealthStatus.DISABLED.value
        provider.last_error = "AI供应商已禁用"
    else:
        try:
            ok, detail = ai_gateway.check(ai_provider_credentials(provider))
            provider.health_status = AiProviderHealthStatus.HEALTHY.value if ok else AiProviderHealthStatus.UNHEALTHY.value
            provider.last_error = "" if ok and "warning" not in detail.lower() else detail
        except Exception as exc:  # noqa: BLE001 - shown to operator.
            provider.health_status = AiProviderHealthStatus.UNHEALTHY.value
            provider.last_error = str(exc)
    provider.updated_at = _now()
    audit(session, tenant_id=None, actor=actor, action="检查AI供应商", target_type="ai_provider", target_id=str(provider.id), detail=provider.health_status)
    session.commit()
    session.refresh(provider)
    return provider


def list_prompt_templates(session: Session, tenant_id: int | None) -> list[PromptTemplate]:
    stmt = select(PromptTemplate)
    if tenant_id is not None:
        stmt = stmt.where(or_(PromptTemplate.tenant_id.is_(None), PromptTemplate.tenant_id == tenant_id))
    return list(session.scalars(stmt.order_by(PromptTemplate.tenant_id.is_(None), PromptTemplate.id.asc())))


def create_prompt_template(session: Session, payload: PromptTemplateCreate, actor: str) -> PromptTemplate:
    template = PromptTemplate(**payload.model_dump())
    session.add(template)
    session.flush()
    audit(session, tenant_id=template.tenant_id, actor=actor, action="新增提示词模板", target_type="prompt_template", target_id=str(template.id))
    session.commit()
    session.refresh(template)
    return template


def update_prompt_template(session: Session, template_id: int, payload: PromptTemplateUpdate, actor: str) -> PromptTemplate:
    template = session.get(PromptTemplate, template_id)
    if not template:
        raise ValueError("prompt template not found")
    data = payload.model_dump(exclude_unset=True)
    for field in ["template_type", "name", "content", "is_active"]:
        if data.get(field) is not None:
            setattr(template, field, data[field])
    if data.get("content") is not None:
        template.version += 1
    template.updated_at = _now()
    audit(session, tenant_id=template.tenant_id, actor=actor, action="更新提示词模板", target_type="prompt_template", target_id=str(template.id))
    session.commit()
    session.refresh(template)
    return template


def get_tenant_ai_setting(session: Session, tenant_id: int) -> TenantAiSetting:
    require_tenant(session, tenant_id)
    setting = session.scalar(select(TenantAiSetting).where(TenantAiSetting.tenant_id == tenant_id))
    if not setting:
        default_provider_id = session.scalar(select(AiProvider.id).order_by(AiProvider.id.asc()))
        setting = TenantAiSetting(tenant_id=tenant_id, default_provider_id=default_provider_id)
        session.add(setting)
        session.commit()
        session.refresh(setting)
    return setting


def update_tenant_ai_setting(session: Session, tenant_id: int, payload: TenantAiSettingUpdate, actor: str) -> TenantAiSetting:
    setting = get_tenant_ai_setting(session, tenant_id)
    data = payload.model_dump(exclude_unset=True)
    for field in ["default_provider_id", "ai_enabled", "fallback_to_mock", "temperature", "max_tokens"]:
        if field in data:
            setattr(setting, field, data[field])
    setting.updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="更新客户AI配置", target_type="tenant_ai_setting", target_id=str(setting.id))
    session.commit()
    session.refresh(setting)
    return setting


def get_scheduling_setting(session: Session, tenant_id: int | None) -> SchedulingSetting:
    setting = session.scalar(select(SchedulingSetting).where(SchedulingSetting.tenant_id == tenant_id))
    if not setting:
        setting = SchedulingSetting(tenant_id=tenant_id)
        session.add(setting)
        session.commit()
        session.refresh(setting)
    return setting


def update_scheduling_setting(session: Session, tenant_id: int | None, payload: SchedulingSettingUpdate, actor: str) -> SchedulingSetting:
    setting = get_scheduling_setting(session, tenant_id)
    data = payload.model_dump(exclude_unset=True)
    for field in ["jitter_min_seconds", "jitter_max_seconds", "batch_interval_seconds", "respect_send_window"]:
        if data.get(field) is not None:
            setattr(setting, field, data[field])
    if setting.jitter_max_seconds < setting.jitter_min_seconds:
        setting.jitter_max_seconds = setting.jitter_min_seconds
    setting.updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="更新发送节奏配置", target_type="scheduling_setting", target_id=str(setting.id))
    session.commit()
    session.refresh(setting)
    return setting


def list_materials(session: Session, tenant_id: int) -> list[Material]:
    return list(session.scalars(select(Material).where(Material.tenant_id == tenant_id).order_by(Material.id.desc())))


def create_material(session: Session, payload: MaterialCreate, actor: str = "普通用户") -> Material:
    require_tenant(session, payload.tenant_id)
    material = Material(**payload.model_dump())
    session.add(material)
    session.flush()
    audit(session, tenant_id=material.tenant_id, actor=actor, action="新增素材", target_type="material", target_id=str(material.id))
    session.commit()
    session.refresh(material)
    return material


def record_ai_usage(
    session: Session,
    *,
    current_user: CurrentUser,
    campaign: Campaign,
    group: TgGroup,
    template: PromptTemplate,
    provider: AiProvider | None,
    provider_name: str,
    model_name: str,
    usage: AiUsage,
    request_status: str,
    error_detail: str = "",
) -> AiUsageLedger:
    input_unit_price = provider.input_price_per_1k if provider else 0.0
    output_unit_price = provider.output_price_per_1k if provider else 0.0
    billable = bool(provider and provider.is_billable and usage.billable)
    total_cost = 0.0
    if billable:
        total_cost = round((usage.prompt_tokens / 1000) * input_unit_price + (usage.completion_tokens / 1000) * output_unit_price, 6)
    ledger = AiUsageLedger(
        tenant_id=campaign.tenant_id,
        user_id=current_user.id,
        campaign_id=campaign.id,
        group_id=group.id,
        provider_id=provider.id if provider else None,
        provider_name=provider_name,
        model_name=model_name,
        prompt_template_id=template.id,
        request_type="campaign_draft_generation",
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        input_unit_price=input_unit_price,
        output_unit_price=output_unit_price,
        total_cost=total_cost,
        currency=provider.currency if provider else "CNY",
        billable=billable,
        request_status=request_status,
        error_detail=error_detail,
    )
    session.add(ledger)
    return ledger


def require_ai_token_balance(session: Session, current_user: CurrentUser) -> None:
    if current_user.is_platform_admin:
        return
    user = session.get(AppUser, current_user.id)
    if not user or not user.is_active:
        raise ValueError("user not found")
    if user.token_balance <= 0:
        raise ValueError("Token 余额不足，请先充值或兑换卡密后再使用 AI")


def deduct_ai_usage_tokens(session: Session, current_user: CurrentUser, usage_ledger: AiUsageLedger) -> UserTokenLedger | None:
    if current_user.is_platform_admin or usage_ledger.total_tokens <= 0:
        return None
    user = session.get(AppUser, current_user.id)
    if not user or not user.is_active:
        raise ValueError("user not found")
    if user.token_balance < usage_ledger.total_tokens:
        raise ValueError("Token 余额不足，请先充值或兑换卡密后再使用 AI")
    consumed = usage_ledger.total_tokens
    user.token_balance -= consumed
    if usage_ledger.campaign_id:
        campaign = session.get(Campaign, usage_ledger.campaign_id)
        if campaign and hasattr(campaign, "used_ai_tokens"):
            campaign.used_ai_tokens += usage_ledger.total_tokens
    ledger = UserTokenLedger(
        tenant_id=user.tenant_id,
        user_id=user.id,
        change_type="ai_usage",
        delta_tokens=-consumed,
        balance_after=user.token_balance,
        related_ai_usage_ledger_id=usage_ledger.id,
        reason=f"{usage_ledger.provider_name}/{usage_ledger.model_name} AI 调用消耗 {usage_ledger.total_tokens} tokens",
        actor=current_user.name,
    )
    session.add(ledger)
    return ledger


def list_usage_ledgers(session: Session, *, user_id: int | None = None, campaign_id: int | None = None) -> list[AiUsageLedger]:
    stmt = select(AiUsageLedger).order_by(AiUsageLedger.id.desc())
    if user_id is not None:
        stmt = stmt.where(AiUsageLedger.user_id == user_id)
    if campaign_id is not None:
        stmt = stmt.where(AiUsageLedger.campaign_id == campaign_id)
    return list(session.scalars(stmt))


def list_usage_summary(session: Session) -> dict:
    ledgers = list_usage_ledgers(session)
    total_requests = len(ledgers)
    successful_requests = sum(1 for item in ledgers if item.request_status == "success")
    failed_requests = total_requests - successful_requests
    billable_requests = sum(1 for item in ledgers if item.billable)
    total_prompt_tokens = sum(item.prompt_tokens for item in ledgers)
    total_completion_tokens = sum(item.completion_tokens for item in ledgers)
    total_tokens = sum(item.total_tokens for item in ledgers)
    total_cost = round(sum(item.total_cost for item in ledgers), 6)
    by_user_map: dict[int, dict] = {}
    for item in ledgers:
        user = session.get(AppUser, item.user_id)
        row = by_user_map.setdefault(
            item.user_id,
            {
                "user_id": item.user_id,
                "user_name": user.name if user else f"user-{item.user_id}",
                "tenant_id": item.tenant_id,
                "requests": 0,
                "total_tokens": 0,
                "total_cost": 0.0,
                "currency": item.currency,
            },
        )
        row["requests"] += 1
        row["total_tokens"] += item.total_tokens
        row["total_cost"] = round(row["total_cost"] + item.total_cost, 6)
    return {
        "total_requests": total_requests,
        "successful_requests": successful_requests,
        "failed_requests": failed_requests,
        "billable_requests": billable_requests,
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_tokens": total_tokens,
        "total_cost": total_cost,
        "currency": "CNY",
        "by_user": list(by_user_map.values()),
    }


def update_material(session: Session, material_id: int, payload: MaterialUpdate, actor: str) -> Material:
    material = session.get(Material, material_id)
    if not material:
        raise ValueError("material not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field in {"id", "tenant_id", "created_at", "updated_at"}:
            continue
        setattr(material, field, value)
    audit(session, tenant_id=material.tenant_id, actor=actor, action="更新素材", target_type="material", target_id=str(material.id))
    session.commit()
    session.refresh(material)
    return material


def list_content_keyword_rules(session: Session, tenant_id: int) -> list[ContentKeywordRule]:
    require_tenant(session, tenant_id)
    return list(
        session.scalars(
            select(ContentKeywordRule)
            .where(ContentKeywordRule.tenant_id == tenant_id)
            .order_by(ContentKeywordRule.is_active.desc(), ContentKeywordRule.id.desc())
        )
    )


def create_content_keyword_rule(session: Session, payload: ContentKeywordRuleCreate, actor: str) -> ContentKeywordRule:
    require_tenant(session, payload.tenant_id)
    keyword = payload.keyword.strip()
    if not keyword:
        raise ValueError("keyword is required")
    exists = session.scalar(
        select(ContentKeywordRule.id).where(
            ContentKeywordRule.tenant_id == payload.tenant_id,
            ContentKeywordRule.keyword == keyword,
        )
    )
    if exists:
        raise ValueError("keyword rule already exists")
    rule = ContentKeywordRule(
        tenant_id=payload.tenant_id,
        keyword=keyword,
        match_type=payload.match_type or "contains",
        is_active=payload.is_active,
        note=payload.note,
    )
    session.add(rule)
    session.flush()
    audit(session, tenant_id=rule.tenant_id, actor=actor, action="新增关键词规则", target_type="content_keyword_rule", target_id=str(rule.id))
    session.commit()
    session.refresh(rule)
    return rule


def update_content_keyword_rule(session: Session, rule_id: int, payload: ContentKeywordRuleUpdate, actor: str) -> ContentKeywordRule:
    rule = session.get(ContentKeywordRule, rule_id)
    if not rule:
        raise ValueError("keyword rule not found")
    data = payload.model_dump(exclude_unset=True)
    if data.get("keyword") is not None:
        keyword = str(data["keyword"]).strip()
        if not keyword:
            raise ValueError("keyword is required")
        duplicate = session.scalar(
            select(ContentKeywordRule.id).where(
                ContentKeywordRule.tenant_id == rule.tenant_id,
                ContentKeywordRule.keyword == keyword,
                ContentKeywordRule.id != rule.id,
            )
        )
        if duplicate:
            raise ValueError("keyword rule already exists")
        rule.keyword = keyword
    for field in ["match_type", "is_active", "note"]:
        if field in data and data[field] is not None:
            setattr(rule, field, data[field])
    rule.updated_at = _now()
    audit(session, tenant_id=rule.tenant_id, actor=actor, action="更新关键词规则", target_type="content_keyword_rule", target_id=str(rule.id))
    session.commit()
    session.refresh(rule)
    return rule


__all__ = [
    "ai_provider_credentials",
    "check_ai_provider",
    "create_ai_provider",
    "create_content_keyword_rule",
    "create_material",
    "create_prompt_template",
    "get_scheduling_setting",
    "get_tenant_ai_setting",
    "list_ai_providers",
    "list_content_keyword_rules",
    "list_materials",
    "list_prompt_templates",
    "list_usage_ledgers",
    "list_usage_summary",
    "record_ai_usage",
    "require_ai_token_balance",
    "seed_ai_configuration",
    "deduct_ai_usage_tokens",
    "update_ai_provider",
    "update_content_keyword_rule",
    "update_material",
    "update_prompt_template",
    "update_scheduling_setting",
    "update_tenant_ai_setting",
]
