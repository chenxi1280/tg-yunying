from __future__ import annotations

import hashlib
import re

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ai_gateway import AiProviderCredentials, AiUsage, normalize_ai_model_name
from app.auth import CurrentUser
from app.models import (
    AccountStatus,
    Action,
    AiProvider,
    AiProviderHealthStatus,
    AiUsageLedger,
    Campaign,
    ContentKeywordRule,
    Material,
    PromptTemplate,
    SchedulingSetting,
    SourceMediaAsset,
    Tenant,
    TenantAiSetting,
    TgAccount,
    TgGroup,
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
from app.schemas.ai_config import MaterialCacheErrorItem, MaterialCacheHealthOut, MaterialCacheStatusCount
from app.security import decrypt_secret, encrypt_secret

from ._common import _now, ai_gateway, audit, require_tenant
from .material_ingestion import URL_MATERIAL_TYPES, save_material_upload_temp, validate_material_url
from .material_versions import record_material_asset_version, record_material_tg_ref_version, record_material_versions

MEDIA_MATERIAL_TYPES = {"图片", "表情包", "文件", "组合消息"}
PUBLIC_MATERIAL_SYSTEM_FIELDS = {
    "asset_fingerprint",
    "cache_ready_status",
    "tg_cache_account_id",
    "tg_cache_peer_id",
    "tg_cache_message_id",
}
EMOJI_ASSET_KINDS = {"", "image_meme", "static_sticker", "animated_sticker", "video_sticker", "custom_emoji"}
MATERIAL_CACHE_STATUSES = {"not_cached", "ready", "refreshing", "flood_wait", "unrecoverable", "cache_failed"}
MATERIAL_DELIVERY_MODES = {"download_reupload"}
CUSTOM_EMOJI_PATTERN = re.compile(r"^custom_emoji:(?P<document_id>\d+):(?P<alt>.+)$")


def _material_fingerprint(payload: MaterialCreate | MaterialUpdate, existing: Material | None = None) -> str:
    explicit = (getattr(payload, "asset_fingerprint", None) or "").strip()
    if explicit:
        return explicit
    content = (getattr(payload, "content", None) or (existing.content if existing else "") or "").strip()
    material_type = (getattr(payload, "material_type", None) or (existing.material_type if existing else "") or "").strip()
    if not content and not material_type:
        return ""
    return hashlib.sha256(f"{material_type}\n{content}".encode("utf-8")).hexdigest()


def _normalize_material_data(data: dict, existing: Material | None = None) -> dict:
    material_type = str(data.get("material_type") if data.get("material_type") is not None else (existing.material_type if existing else "文本"))
    if data.get("delivery_mode") in {None, ""}:
        data["delivery_mode"] = existing.delivery_mode if existing else "download_reupload"
    if data["delivery_mode"] not in MATERIAL_DELIVERY_MODES:
        raise ValueError("素材发送方式仅支持 download_reupload")
    if data.get("cache_ready_status") in {None, ""}:
        data["cache_ready_status"] = existing.cache_ready_status if existing else "not_cached"
    if data["cache_ready_status"] not in MATERIAL_CACHE_STATUSES:
        raise ValueError("素材缓存状态无效")
    emoji_kind = str(data.get("emoji_asset_kind") if data.get("emoji_asset_kind") is not None else (existing.emoji_asset_kind if existing else "") or "")
    if emoji_kind not in EMOJI_ASSET_KINDS:
        raise ValueError("表情包子类型无效")
    if material_type == "表情包" and not emoji_kind:
        data["emoji_asset_kind"] = "image_meme"
    elif "emoji_asset_kind" not in data:
        data["emoji_asset_kind"] = emoji_kind
    if material_type == "表情包" and data.get("emoji_asset_kind") == "custom_emoji":
        content = str(data.get("content") if data.get("content") is not None else (existing.content if existing else "") or "").strip()
        if not CUSTOM_EMOJI_PATTERN.match(content):
            raise ValueError("custom emoji 素材格式应为 custom_emoji:<document_id>:<alt>")
    if data.get("source_kind") in {None, ""}:
        data["source_kind"] = existing.source_kind if existing else "url"
    if data.get("gateway_type") in {None, ""}:
        data["gateway_type"] = existing.gateway_type if existing else "telethon"
    should_refresh_fingerprint = existing is None or any(field in data for field in {"material_type", "content", "asset_fingerprint"})
    if should_refresh_fingerprint and not data.get("asset_fingerprint"):
        material_payload = MaterialCreate(**{**_material_defaults(existing), **data}) if existing is None else MaterialUpdate(**data)
        data["asset_fingerprint"] = _material_fingerprint(material_payload, existing)
    cache_peer = str(data.get("tg_cache_peer_id") if data.get("tg_cache_peer_id") is not None else (existing.tg_cache_peer_id if existing else "") or "")
    cache_message_id = str(data.get("tg_cache_message_id") if data.get("tg_cache_message_id") is not None else (existing.tg_cache_message_id if existing else "") or "")
    has_tg_cache_ref = bool(cache_peer.strip() and cache_message_id.strip())
    if material_type == "表情包" and data.get("emoji_asset_kind") == "custom_emoji":
        data["cache_ready_status"] = "ready"
        data["tg_cache_account_id"] = None
        data["tg_cache_peer_id"] = ""
        data["tg_cache_message_id"] = ""
    elif material_type in MEDIA_MATERIAL_TYPES:
        if has_tg_cache_ref and data["cache_ready_status"] in {"", "not_cached"}:
            data["cache_ready_status"] = "ready"
        elif not has_tg_cache_ref and data["cache_ready_status"] == "ready":
            data["cache_ready_status"] = "not_cached"
    else:
        data["cache_ready_status"] = "ready"
    return data


def _material_defaults(existing: Material | None) -> dict:
    if existing is None:
        return {}
    return {
        "tenant_id": existing.tenant_id,
        "title": existing.title,
        "material_type": existing.material_type,
        "content": existing.content,
        "tags": existing.tags,
        "review_status": existing.review_status,
        "source_kind": existing.source_kind,
        "asset_fingerprint": existing.asset_fingerprint,
        "delivery_mode": existing.delivery_mode,
        "emoji_asset_kind": existing.emoji_asset_kind,
        "gateway_type": existing.gateway_type,
        "cache_ready_status": existing.cache_ready_status,
        "tg_cache_account_id": existing.tg_cache_account_id,
        "tg_cache_peer_id": existing.tg_cache_peer_id,
        "tg_cache_message_id": existing.tg_cache_message_id,
        "file_name": existing.file_name,
        "mime_type": existing.mime_type,
        "file_size": existing.file_size,
        "width": existing.width,
        "height": existing.height,
        "caption": existing.caption,
    }


def _sanitize_public_material_create(data: dict) -> dict:
    for field in PUBLIC_MATERIAL_SYSTEM_FIELDS:
        data.pop(field, None)
    material_type = str(data.get("material_type") or "文本")
    emoji_kind = str(data.get("emoji_asset_kind") or "")
    if material_type in URL_MATERIAL_TYPES and not (material_type == "表情包" and emoji_kind == "custom_emoji"):
        data["content"] = validate_material_url(str(data.get("content") or ""), material_type=material_type)
        data["source_kind"] = "url"
        data["cache_ready_status"] = "not_cached"
        data["tg_cache_account_id"] = None
        data["tg_cache_peer_id"] = ""
        data["tg_cache_message_id"] = ""
    return data


def _sanitize_public_material_update(data: dict, material: Material) -> tuple[dict, bool]:
    for field in PUBLIC_MATERIAL_SYSTEM_FIELDS:
        data.pop(field, None)
    content_changed = any(field in data for field in {"material_type", "content", "asset_fingerprint"})
    material_type = str(data.get("material_type") or material.material_type)
    content = str(data.get("content") if data.get("content") is not None else material.content)
    emoji_kind = str(data.get("emoji_asset_kind") if data.get("emoji_asset_kind") is not None else material.emoji_asset_kind or "")
    if material_type in URL_MATERIAL_TYPES and content_changed and not (material_type == "表情包" and emoji_kind == "custom_emoji"):
        data["content"] = validate_material_url(content, material_type=material_type)
        data["source_kind"] = "url"
        data["cache_ready_status"] = "not_cached"
        data["tg_cache_account_id"] = None
        data["tg_cache_peer_id"] = ""
        data["tg_cache_message_id"] = ""
    return data, content_changed


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
            template_type="AI黑话词表",
            name="默认 AI 黑话词表",
            content=(
                "本模板用于 AI 活群任务的行业黑话/俗语口径。每行写一个映射，格式：原词=实际含义。\n"
                "示例：\n"
                "老师=妓女\n"
                "开课=开始营业"
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
    for field in [
        "jitter_min_seconds",
        "jitter_max_seconds",
        "batch_interval_seconds",
        "respect_send_window",
        "quiet_hours_enabled",
        "quiet_start",
        "quiet_end",
        "quiet_timezone",
        "default_max_retries",
        "default_retry_delay_seconds",
        "default_retry_backoff",
        "default_on_account_banned",
        "default_on_api_rate_limit",
        "default_on_content_rejected",
        "default_account_hour_limit",
        "default_account_day_limit",
        "default_account_cooldown_seconds",
    ]:
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
    data = _sanitize_public_material_create(payload.model_dump())
    material = Material(**_normalize_material_data(data))
    session.add(material)
    session.flush()
    record_material_versions(session, material, actor=actor)
    audit(session, tenant_id=material.tenant_id, actor=actor, action="新增素材", target_type="material", target_id=str(material.id))
    session.commit()
    session.refresh(material)
    return material


def create_uploaded_material(
    session: Session,
    *,
    tenant_id: int,
    title: str,
    material_type: str,
    tags: str,
    caption: str,
    filename: str,
    content_type: str,
    data: bytes,
    emoji_asset_kind: str = "",
    actor: str = "普通用户",
) -> Material:
    require_tenant(session, tenant_id)
    path, normalized_type, fingerprint = save_material_upload_temp(
        tenant_id=tenant_id,
        filename=filename,
        content_type=content_type,
        data=data,
        material_type=material_type,
    )
    material = Material(
        **_normalize_material_data(
            {
                "tenant_id": tenant_id,
                "title": title.strip(),
                "material_type": material_type,
                "content": str(path),
                "tags": tags.strip(),
                "review_status": "已审核",
                "source_kind": "upload",
                "asset_fingerprint": fingerprint,
                "delivery_mode": "download_reupload",
                "emoji_asset_kind": emoji_asset_kind or ("image_meme" if material_type == "表情包" else ""),
                "gateway_type": "telethon",
                "cache_ready_status": "not_cached",
                "file_name": filename or path.name,
                "mime_type": normalized_type,
                "file_size": len(data),
                "caption": caption.strip(),
            }
        )
    )
    session.add(material)
    session.flush()
    record_material_versions(session, material, actor=actor)
    audit(session, tenant_id=material.tenant_id, actor=actor, action="上传素材", target_type="material", target_id=str(material.id))
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
    return


def deduct_ai_usage_tokens(session: Session, current_user: CurrentUser, usage_ledger: AiUsageLedger):
    if usage_ledger.campaign_id:
        campaign = session.get(Campaign, usage_ledger.campaign_id)
        if campaign and hasattr(campaign, "used_ai_tokens"):
            campaign.used_ai_tokens += usage_ledger.total_tokens
    return None


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
    by_actor_map: dict[int, dict] = {}
    for item in ledgers:
        row = by_actor_map.setdefault(
            item.user_id,
            {
                "user_id": item.user_id,
                "user_name": "系统管理员" if item.user_id == 0 else f"actor-{item.user_id}",
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
        "by_user": list(by_actor_map.values()),
    }


def update_material(session: Session, material_id: int, payload: MaterialUpdate, actor: str) -> Material:
    material = session.get(Material, material_id)
    if not material:
        raise ValueError("material not found")
    raw_data = payload.model_dump(exclude_unset=True)
    data, content_changed = _sanitize_public_material_update(raw_data, material)
    media_content_changed = content_changed and str(data.get("material_type") or material.material_type) in MEDIA_MATERIAL_TYPES
    if media_content_changed:
        data.update(
            {
                "cache_ready_status": "not_cached",
                "tg_cache_account_id": None,
                "tg_cache_peer_id": "",
                "tg_cache_message_id": "",
            }
        )
    data = _normalize_material_data(data, material)
    for field, value in data.items():
        if field in {"id", "tenant_id", "created_at", "updated_at"}:
            continue
        setattr(material, field, value)
    if content_changed:
        material.asset_version_id += 1
    if media_content_changed:
        material.tg_ref_version_id += 1
        material.last_cache_error = ""
    if content_changed:
        record_material_asset_version(session, material, actor=actor)
    if media_content_changed:
        record_material_tg_ref_version(session, material, actor=actor)
    audit(session, tenant_id=material.tenant_id, actor=actor, action="更新素材", target_type="material", target_id=str(material.id))
    session.commit()
    session.refresh(material)
    return material


def material_cache_health(session: Session, tenant_id: int) -> MaterialCacheHealthOut:
    require_tenant(session, tenant_id)

    def count_rows(model, status_field, where_tenant) -> list[MaterialCacheStatusCount]:
        rows = session.execute(
            select(status_field, func.count()).where(where_tenant).group_by(status_field)
        ).all()
        return [MaterialCacheStatusCount(status=str(status or ""), count=int(count)) for status, count in rows]

    material_counts = count_rows(Material, Material.cache_ready_status, Material.tenant_id == tenant_id)
    source_counts = count_rows(SourceMediaAsset, SourceMediaAsset.cache_status, SourceMediaAsset.tenant_id == tenant_id)
    material_oldest = session.scalar(
        select(func.min(Material.last_cache_flood_wait_until)).where(
            Material.tenant_id == tenant_id,
            Material.cache_ready_status.in_(["not_cached", "refreshing", "flood_wait", "cache_failed"]),
        )
    )
    source_oldest = session.scalar(
        select(func.min(SourceMediaAsset.created_at)).where(
            SourceMediaAsset.tenant_id == tenant_id,
            SourceMediaAsset.cache_status.in_(["pending_cache", "cache_flood_wait", "cache_failed"]),
        )
    )
    active_accounts = session.scalar(
        select(func.count(TgAccount.id)).where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.status == AccountStatus.ACTIVE.value,
        )
    ) or 0
    waiting_actions = session.scalar(
        select(func.count(Action.id)).where(
            Action.tenant_id == tenant_id,
            Action.status == "waiting_cache",
        )
    ) or 0
    material_errors = session.scalars(
        select(Material)
        .where(
            Material.tenant_id == tenant_id,
            Material.last_cache_error != "",
        )
        .order_by(Material.id.desc())
        .limit(5)
    ).all()
    source_errors = session.scalars(
        select(SourceMediaAsset)
        .where(
            SourceMediaAsset.tenant_id == tenant_id,
            SourceMediaAsset.failure_reason != "",
        )
        .order_by(SourceMediaAsset.updated_at.desc())
        .limit(5)
    ).all()
    recent_errors: list[MaterialCacheErrorItem] = [
        MaterialCacheErrorItem(scope="material", id=str(item.id), title=item.title, status=item.cache_ready_status, reason=item.last_cache_error)
        for item in material_errors
    ]
    recent_errors.extend(
        MaterialCacheErrorItem(
            scope="source_media",
            id=item.id,
            title=item.source_message_id or item.media_fingerprint or "source_media",
            status=item.cache_status,
            reason=item.failure_reason,
        )
        for item in source_errors
    )
    flood_wait_count = sum(row.count for row in material_counts if row.status == "flood_wait") + sum(row.count for row in source_counts if row.status == "cache_flood_wait")
    cache_failed_count = sum(row.count for row in material_counts if row.status in {"cache_failed", "unrecoverable"}) + sum(row.count for row in source_counts if row.status in {"cache_failed", "unrecoverable"})
    from app.config import get_settings

    settings = get_settings()
    return MaterialCacheHealthOut(
        material_cache_peer_configured=bool(settings.material_cache_peer_id),
        source_media_cache_peer_configured=bool(settings.source_media_cache_peer_id),
        active_cache_account_count=int(active_accounts),
        material_status_counts=material_counts,
        source_media_status_counts=source_counts,
        material_oldest_pending_at=material_oldest,
        source_media_oldest_pending_at=source_oldest,
        flood_wait_count=flood_wait_count,
        cache_failed_count=cache_failed_count,
        waiting_action_count=int(waiting_actions),
        recent_errors=recent_errors[:10],
    )


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
    audit(session, tenant_id=rule.tenant_id, actor=actor, action="新增旧内容过滤规则", target_type="content_keyword_rule", target_id=str(rule.id))
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
    audit(session, tenant_id=rule.tenant_id, actor=actor, action="更新旧内容过滤规则", target_type="content_keyword_rule", target_id=str(rule.id))
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
