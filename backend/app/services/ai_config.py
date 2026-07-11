from __future__ import annotations

import hashlib
import re
import zipfile
from collections import defaultdict
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ai_gateway import AiProviderCredentials, AiUsage, normalize_ai_model_name
from app.auth import CurrentUser
from app.config import get_settings
from app.models import (
    AccountStatus,
    Action,
    AiProvider,
    AiProviderHealthStatus,
    AiUsageLedger,
    Campaign,
    ContentKeywordRule,
    Material,
    MaterialCacheConfig,
    MaterialGroup,
    MaterialImportJob,
    MaterialAssetVersion,
    MaterialTgRefVersion,
    MessageTask,
    OperationPlanGenerationRun,
    OperationPlanTarget,
    OperationPlanTemplate,
    PromptTemplate,
    RuleSetVersion,
    SchedulingSetting,
    SourceMediaAsset,
    Tenant,
    TenantAiSetting,
    TgAccountSecurityBatchItem,
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
from app.schemas.ai_config import (
    CacheChannelConfigOut,
    CacheExecutionAccountOut,
    DEFAULT_AI_MAX_TOKENS_LIMIT,
    MaterialCacheConfigOut,
    MaterialCacheErrorItem,
    MaterialCacheHealthOut,
    MaterialCacheStatusCount,
    MINIMAX_MAX_TOKENS_LIMIT,
    MaterialGroupCreate,
    MaterialGroupOut,
    MaterialGroupUpdate,
    MaterialImportItemOut,
    MaterialImportResultOut,
    MaterialReferencesOut,
    MaterialReferenceItemOut,
    MaterialReferenceSummary,
    MaterialVersionHistoryOut,
)
from app.security import decrypt_secret, encrypt_secret

from ._common import _now, ai_gateway, audit, gateway, require_tenant
from .material_ingestion import URL_MATERIAL_TYPES, save_material_upload_temp, validate_material_url
from .material_versions import record_material_asset_version, record_material_tg_ref_version, record_material_versions

MEDIA_MATERIAL_TYPES = {"图片", "表情包", "文件", "组合消息"}
CACHE_REFRESH_MATERIAL_TYPES = {"图片", "表情包", "文件"}
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
ZIP_IMAGE_MAX_BYTES = 500 * 1024


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
                "每条优先 8-24 个字，围绕上次经历、到场感受、时间位置、照片差异、回访、下次安排或小疑问；"
                "可以半句、省主语、少标点，不要写总结、广告语、完整观点。"
                "禁止模板味表达：这个话题、自然接一句、换个角度、轻量推进、大家怎么看、可以继续聊聊。"
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
                "写法像群里随手回：短、碎、具体，多用“我上次那个”“走之前”“结束后”“下次试试”这类现场感，"
                "少用句号逗号，不要总结上下文，不要说这个话题、自然接一句、换个角度。"
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
        return _save_ai_provider_check_result(session, provider, actor, AiProviderHealthStatus.DISABLED.value, "AI供应商已禁用")
    try:
        credentials = ai_provider_credentials(provider)
    except Exception as exc:  # noqa: BLE001 - shown to operator.
        return _save_ai_provider_check_result(session, provider, actor, AiProviderHealthStatus.UNHEALTHY.value, str(exc))
    session.rollback()
    try:
        ok, detail = ai_gateway.check(credentials)
        health_status = AiProviderHealthStatus.HEALTHY.value if ok else AiProviderHealthStatus.UNHEALTHY.value
        last_error = "" if ok and "warning" not in detail.lower() else detail
    except Exception as exc:  # noqa: BLE001 - shown to operator.
        health_status = AiProviderHealthStatus.UNHEALTHY.value
        last_error = str(exc)
    provider = session.get(AiProvider, provider_id)
    if not provider:
        raise ValueError("ai provider not found")
    return _save_ai_provider_check_result(session, provider, actor, health_status, last_error)


def _save_ai_provider_check_result(
    session: Session,
    provider: AiProvider,
    actor: str,
    health_status: str,
    last_error: str,
) -> AiProvider:
    provider.last_check_at = _now()
    provider.health_status = health_status
    provider.last_error = last_error
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
    provider_id = data.get("default_provider_id", setting.default_provider_id)
    _validate_tenant_ai_token_limit(session, provider_id, data.get("max_tokens"))
    for field in [
        "default_provider_id",
        "ai_enabled",
        "fallback_to_mock",
        "ai_group_model_fallback_enabled",
        "ai_group_grok_fallback_enabled",
        "ai_group_static_fallback_enabled",
        "temperature",
        "max_tokens",
    ]:
        if field in data:
            setattr(setting, field, data[field])
    setting.updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="更新客户AI配置", target_type="tenant_ai_setting", target_id=str(setting.id))
    session.commit()
    session.refresh(setting)
    return setting


def _validate_tenant_ai_token_limit(session: Session, provider_id: int | None, max_tokens: int | None) -> None:
    if max_tokens is None:
        return
    provider = session.get(AiProvider, provider_id) if provider_id else None
    limit = _tenant_ai_token_limit(provider)
    if max_tokens > limit:
        raise ValueError(f"最大 Token 超过当前模型上限：{limit}")


def _tenant_ai_token_limit(provider: AiProvider | None) -> int:
    if provider and _is_minimax_provider(provider):
        return MINIMAX_MAX_TOKENS_LIMIT
    return DEFAULT_AI_MAX_TOKENS_LIMIT


def _is_minimax_provider(provider: AiProvider) -> bool:
    text = f"{provider.provider_name} {provider.base_url} {provider.model_name}".lower()
    return "minimax" in text or "minimaxi" in text


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


def _material_id_matches(value: Any, material_id: int) -> bool:
    if value is None:
        return False
    material_id_text = str(material_id)
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == material_id
    if isinstance(value, str):
        parts = [part.strip().removeprefix("material:") for part in re.split(r"[,，\s]+", value) if part.strip()]
        return material_id_text in parts
    if isinstance(value, list | tuple | set):
        return any(_material_id_matches(item, material_id) for item in value)
    return False


def _material_ids_from_value(value: Any, material_ids: set[int], *, material_key: bool = False) -> set[int]:
    if value is None or isinstance(value, bool):
        return set()
    if isinstance(value, int):
        return {value} if material_key and value in material_ids else set()
    if isinstance(value, str):
        if not material_key:
            return set()
        matched: set[int] = set()
        for part in (item.strip().removeprefix("material:") for item in re.split(r"[,，\s]+", value) if item.strip()):
            if part.isdigit():
                parsed = int(part)
                if parsed in material_ids:
                    matched.add(parsed)
        return matched
    if isinstance(value, dict):
        matched: set[int] = set()
        for key, item in value.items():
            matched.update(_material_ids_from_value(item, material_ids, material_key=key in {"material_id", "material_ids", "materials", "avatar_source"}))
        return matched
    if isinstance(value, list | tuple | set):
        matched: set[int] = set()
        for item in value:
            matched.update(_material_ids_from_value(item, material_ids, material_key=material_key))
        return matched
    return set()


def _json_mentions_material(value: Any, material_id: int) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"material_id", "material_ids", "materials", "avatar_source"} and _material_id_matches(item, material_id):
                return True
            if _json_mentions_material(item, material_id):
                return True
        return False
    if isinstance(value, list | tuple | set):
        return any(_json_mentions_material(item, material_id) for item in value)
    return False


def _count_json_references(session: Session, stmt, fields: list[str], material_id: int) -> int:
    count = 0
    for row in session.scalars(stmt):
        for field in fields:
            if _json_mentions_material(getattr(row, field, None), material_id):
                count += 1
                break
    return count


def _count_json_references_for_materials(session: Session, stmt, fields: list[str], material_ids: set[int]) -> dict[int, int]:
    counts: dict[int, int] = defaultdict(int)
    if not material_ids:
        return {}
    for row in session.scalars(stmt):
        row_material_ids: set[int] = set()
        for field in fields:
            row_material_ids.update(_material_ids_from_value(getattr(row, field, None), material_ids))
        for material_id in row_material_ids:
            counts[material_id] += 1
    return dict(counts)


def material_reference_summary(session: Session, tenant_id: int, material_id: int) -> MaterialReferenceSummary:
    message_task_count = session.scalar(
        select(func.count(MessageTask.id)).where(
            MessageTask.tenant_id == tenant_id,
            MessageTask.material_id == material_id,
        )
    ) or 0
    action_count = _count_json_references(
        session,
        select(Action).where(Action.tenant_id == tenant_id),
        ["payload", "result"],
        material_id,
    )
    rule_version_count = _count_json_references(
        session,
        select(RuleSetVersion).where(RuleSetVersion.tenant_id == tenant_id),
        ["filters", "output_checks", "transforms", "routing", "account_strategy", "rate_limits", "retry_policy"],
        material_id,
    )
    operation_plan_count = (
        _count_json_references(
            session,
            select(OperationPlanTemplate).where(OperationPlanTemplate.tenant_id == tenant_id),
            ["strategy_config", "task_blueprints"],
            material_id,
        )
        + _count_json_references(
            session,
            select(OperationPlanTarget).where(OperationPlanTarget.tenant_id == tenant_id),
            ["strategy_config"],
            material_id,
        )
        + _count_json_references(
            session,
            select(OperationPlanGenerationRun).where(OperationPlanGenerationRun.tenant_id == tenant_id),
            ["request_payload", "result_payload"],
            material_id,
        )
    )
    account_profile_batch_count = session.scalar(
        select(func.count(TgAccountSecurityBatchItem.id)).where(
            TgAccountSecurityBatchItem.tenant_id == tenant_id,
            TgAccountSecurityBatchItem.avatar_source.in_([str(material_id), f"material:{material_id}"]),
        )
    ) or 0
    total_count = int(message_task_count) + action_count + rule_version_count + operation_plan_count + int(account_profile_batch_count)
    return MaterialReferenceSummary(
        message_task_count=int(message_task_count),
        action_count=action_count,
        rule_version_count=rule_version_count,
        operation_plan_count=operation_plan_count,
        account_profile_batch_count=int(account_profile_batch_count),
        total_count=total_count,
    )


def material_reference_summaries(session: Session, tenant_id: int, material_ids: list[int]) -> dict[int, MaterialReferenceSummary]:
    material_id_set = set(material_ids)
    summaries = {material_id: MaterialReferenceSummary() for material_id in material_ids}
    if not material_id_set:
        return summaries

    for material_id, count in session.execute(
        select(MessageTask.material_id, func.count(MessageTask.id))
        .where(MessageTask.tenant_id == tenant_id, MessageTask.material_id.in_(material_id_set))
        .group_by(MessageTask.material_id)
    ):
        summaries[int(material_id)].message_task_count = int(count or 0)

    for material_id, count in _count_json_references_for_materials(
        session,
        select(Action).where(Action.tenant_id == tenant_id),
        ["payload", "result"],
        material_id_set,
    ).items():
        summaries[material_id].action_count = count

    for material_id, count in _count_json_references_for_materials(
        session,
        select(RuleSetVersion).where(RuleSetVersion.tenant_id == tenant_id),
        ["filters", "output_checks", "transforms", "routing", "account_strategy", "rate_limits", "retry_policy"],
        material_id_set,
    ).items():
        summaries[material_id].rule_version_count = count

    operation_plan_counts: dict[int, int] = defaultdict(int)
    for stmt, fields in (
        (select(OperationPlanTemplate).where(OperationPlanTemplate.tenant_id == tenant_id), ["strategy_config", "task_blueprints"]),
        (select(OperationPlanTarget).where(OperationPlanTarget.tenant_id == tenant_id), ["strategy_config"]),
        (select(OperationPlanGenerationRun).where(OperationPlanGenerationRun.tenant_id == tenant_id), ["request_payload", "result_payload"]),
    ):
        for material_id, count in _count_json_references_for_materials(session, stmt, fields, material_id_set).items():
            operation_plan_counts[material_id] += count
    for material_id, count in operation_plan_counts.items():
        summaries[material_id].operation_plan_count = int(count)

    for avatar_source, count in session.execute(
        select(TgAccountSecurityBatchItem.avatar_source, func.count(TgAccountSecurityBatchItem.id))
        .where(
            TgAccountSecurityBatchItem.tenant_id == tenant_id,
            TgAccountSecurityBatchItem.avatar_source.in_([item for material_id in material_id_set for item in (str(material_id), f"material:{material_id}")]),
        )
        .group_by(TgAccountSecurityBatchItem.avatar_source)
    ):
        matched_ids = _material_ids_from_value(avatar_source, material_id_set, material_key=True)
        for material_id in matched_ids:
            summaries[material_id].account_profile_batch_count += int(count or 0)

    for summary in summaries.values():
        summary.total_count = (
            summary.message_task_count
            + summary.action_count
            + summary.rule_version_count
            + summary.operation_plan_count
            + summary.account_profile_batch_count
        )
    return summaries


def _attach_material_reference_summary(session: Session, material: Material) -> Material:
    summary = material_reference_summary(session, material.tenant_id, material.id)
    material.reference_summary = summary
    material.referenced_by_count = summary.total_count
    return material


def get_material(session: Session, tenant_id: int, material_id: int) -> Material:
    require_tenant(session, tenant_id)
    material = session.get(Material, material_id)
    if not material or material.tenant_id != tenant_id:
        raise ValueError("material not found")
    return _attach_material_reference_summary(session, material)


def list_materials(session: Session, tenant_id: int) -> list[Material]:
    materials = list(session.scalars(select(Material).where(Material.tenant_id == tenant_id).order_by(Material.id.desc())))
    summaries = material_reference_summaries(session, tenant_id, [material.id for material in materials])
    for material in materials:
        summary = summaries.get(material.id, MaterialReferenceSummary())
        material.reference_summary = summary
        material.referenced_by_count = summary.total_count
    return materials


def list_material_version_history(session: Session, tenant_id: int, material_id: int) -> MaterialVersionHistoryOut:
    material = get_material(session, tenant_id, material_id)
    asset_versions = list(
        session.scalars(
            select(MaterialAssetVersion)
            .where(MaterialAssetVersion.tenant_id == tenant_id, MaterialAssetVersion.material_id == material.id)
            .order_by(MaterialAssetVersion.asset_version_id.desc())
        )
    )
    tg_ref_versions = list(
        session.scalars(
            select(MaterialTgRefVersion)
            .where(MaterialTgRefVersion.tenant_id == tenant_id, MaterialTgRefVersion.material_id == material.id)
            .order_by(MaterialTgRefVersion.tg_ref_version_id.desc())
        )
    )
    return MaterialVersionHistoryOut(material_id=material.id, asset_versions=asset_versions, tg_ref_versions=tg_ref_versions)


def material_references(session: Session, tenant_id: int, material_id: int) -> MaterialReferencesOut:
    material = get_material(session, tenant_id, material_id)
    items: list[MaterialReferenceItemOut] = []
    for task in session.scalars(select(MessageTask).where(MessageTask.tenant_id == tenant_id, MessageTask.material_id == material.id).order_by(MessageTask.id.desc()).limit(50)):
        items.append(MaterialReferenceItemOut(source_type="message_task", source_id=str(task.id), title=task.content[:80], status=task.status))
    for action in session.scalars(select(Action).where(Action.tenant_id == tenant_id).order_by(Action.created_at.desc()).limit(200)):
        if _json_mentions_material(action.payload, material.id) or _json_mentions_material(action.result, material.id):
            items.append(MaterialReferenceItemOut(source_type="action", source_id=str(action.id), title=action.action_type, status=action.status))
            if len(items) >= 100:
                break
    return MaterialReferencesOut(material_id=material.id, summary=material.reference_summary, items=items)


def refresh_material_cache(session: Session, tenant_id: int, material_id: int, actor: str, *, reason: str = "") -> Material:
    material = get_material(session, tenant_id, material_id)
    if material.material_type not in CACHE_REFRESH_MATERIAL_TYPES:
        raise ValueError("该素材类型不支持刷新缓存")
    material.cache_ready_status = "not_cached"
    material.tg_cache_account_id = None
    material.tg_cache_peer_id = ""
    material.tg_cache_message_id = ""
    material.last_cache_error = ""
    material.tg_ref_version_id += 1
    material.updated_at = _now()
    record_material_tg_ref_version(session, material, actor=actor)
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="刷新素材缓存",
        target_type="material",
        target_id=str(material.id),
        detail=f"reason={(reason or '').strip() or '未填写'}; tg_ref_version={material.tg_ref_version_id}",
    )
    session.commit()
    session.refresh(material)
    return _attach_material_reference_summary(session, material)


def _material_group_out(session: Session, group: MaterialGroup) -> MaterialGroupOut:
    material_count = session.scalar(
        select(func.count(Material.id)).where(
            Material.tenant_id == group.tenant_id,
            Material.material_type == group.group_type,
        )
    ) if group.group_type else 0
    return MaterialGroupOut(
        id=group.id,
        tenant_id=group.tenant_id,
        name=group.name,
        group_type=group.group_type,
        description=group.description,
        is_active=group.is_active,
        material_count=int(material_count or 0),
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


def list_material_groups(session: Session, tenant_id: int) -> list[MaterialGroupOut]:
    require_tenant(session, tenant_id)
    rows = list(session.scalars(select(MaterialGroup).where(MaterialGroup.tenant_id == tenant_id).order_by(MaterialGroup.id.asc())))
    return [_material_group_out(session, group) for group in rows]


def create_material_group(session: Session, tenant_id: int, payload: MaterialGroupCreate, actor: str) -> MaterialGroupOut:
    require_tenant(session, tenant_id)
    name = payload.name.strip()
    if session.scalar(select(MaterialGroup.id).where(MaterialGroup.tenant_id == tenant_id, MaterialGroup.name == name)):
        raise ValueError("material group already exists")
    group = MaterialGroup(
        tenant_id=tenant_id,
        name=name,
        group_type=(payload.group_type or "").strip(),
        description=(payload.description or "").strip(),
        is_active=payload.is_active,
    )
    session.add(group)
    session.flush()
    audit(session, tenant_id=tenant_id, actor=actor, action="新增素材分组", target_type="material_group", target_id=str(group.id), detail=group.name)
    session.commit()
    session.refresh(group)
    return _material_group_out(session, group)


def update_material_group(session: Session, tenant_id: int, group_id: int, payload: MaterialGroupUpdate, actor: str) -> MaterialGroupOut:
    require_tenant(session, tenant_id)
    group = session.get(MaterialGroup, group_id)
    if not group or group.tenant_id != tenant_id:
        raise ValueError("material group not found")
    data = payload.model_dump(exclude_unset=True)
    if "name" in data and data["name"] is not None:
        name = str(data["name"]).strip()
        existing_id = session.scalar(select(MaterialGroup.id).where(MaterialGroup.tenant_id == tenant_id, MaterialGroup.name == name, MaterialGroup.id != group.id))
        if existing_id:
            raise ValueError("material group already exists")
        group.name = name
    if "group_type" in data and data["group_type"] is not None:
        group.group_type = str(data["group_type"]).strip()
    if "description" in data and data["description"] is not None:
        group.description = str(data["description"]).strip()
    if "is_active" in data and data["is_active"] is not None:
        group.is_active = bool(data["is_active"])
    group.updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="更新素材分组", target_type="material_group", target_id=str(group.id), detail=group.name)
    session.commit()
    session.refresh(group)
    return _material_group_out(session, group)


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
    return _attach_material_reference_summary(session, material)


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
    material = _new_uploaded_material(
        tenant_id=tenant_id,
        title=title,
        material_type=material_type,
        tags=tags,
        caption=caption,
        filename=filename,
        content_type=content_type,
        data=data,
        emoji_asset_kind=emoji_asset_kind,
    )
    session.add(material)
    session.flush()
    record_material_versions(session, material, actor=actor)
    audit(session, tenant_id=material.tenant_id, actor=actor, action="上传素材", target_type="material", target_id=str(material.id))
    session.commit()
    session.refresh(material)
    return _attach_material_reference_summary(session, material)


def create_uploaded_materials(
    session: Session,
    *,
    tenant_id: int,
    title: str,
    material_type: str,
    tags: str,
    caption: str,
    files: list[tuple[str, str, bytes]],
    emoji_asset_kind: str = "",
    actor: str = "普通用户",
) -> list[Material]:
    require_tenant(session, tenant_id)
    if not files:
        raise ValueError("请至少选择一个素材文件")
    materials: list[Material] = []
    multiple = len(files) > 1
    base_title = title.strip() or "素材"
    for index, (filename, content_type, data) in enumerate(files, start=1):
        resolved_title = _uploaded_material_title(base_title, filename, index=index, multiple=multiple)
        material = _new_uploaded_material(
            tenant_id=tenant_id,
            title=resolved_title,
            material_type=material_type,
            tags=tags,
            caption=caption,
            filename=filename,
            content_type=content_type,
            data=data,
            emoji_asset_kind=emoji_asset_kind,
        )
        session.add(material)
        materials.append(material)
    session.flush()
    for material in materials:
        record_material_versions(session, material, actor=actor)
        audit(session, tenant_id=material.tenant_id, actor=actor, action="批量上传素材", target_type="material", target_id=str(material.id))
    session.commit()
    for material in materials:
        session.refresh(material)
        _attach_material_reference_summary(session, material)
    return materials


def _zip_content_type(filename: str, data: bytes) -> str:
    suffix = str(filename or "").rsplit(".", 1)[-1].lower()
    if suffix in {"jpg", "jpeg"} and data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if suffix == "png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return ""


def _material_import_result(job: MaterialImportJob) -> MaterialImportResultOut:
    return MaterialImportResultOut(
        import_id=job.id,
        source_filename=job.source_filename,
        import_type=job.import_type,
        target_group_name=job.target_group_name,
        status=job.status,
        total_count=job.total_count,
        success_count=job.success_count,
        failed_count=job.failed_count,
        skipped_count=job.skipped_count,
        duplicate_count=job.duplicate_count,
        oversize_count=job.oversize_count,
        items=[MaterialImportItemOut(**item) for item in (job.item_details or [])],
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def get_material_import_result(session: Session, *, tenant_id: int, import_id: str) -> MaterialImportResultOut:
    require_tenant(session, tenant_id)
    job = session.get(MaterialImportJob, import_id)
    if not job or job.tenant_id != tenant_id:
        raise ValueError("material import not found")
    return _material_import_result(job)


def list_material_import_results(session: Session, *, tenant_id: int, limit: int = 20) -> list[MaterialImportResultOut]:
    require_tenant(session, tenant_id)
    rows = session.scalars(
        select(MaterialImportJob)
        .where(MaterialImportJob.tenant_id == tenant_id)
        .order_by(MaterialImportJob.created_at.desc())
        .limit(max(1, min(limit, 100)))
    ).all()
    return [_material_import_result(job) for job in rows]


def create_material_zip_import(
    session: Session,
    *,
    tenant_id: int,
    title: str,
    material_type: str,
    tags: str,
    caption: str,
    filename: str,
    data: bytes,
    actor: str = "普通用户",
) -> MaterialImportResultOut:
    require_tenant(session, tenant_id)
    if not data:
        raise ValueError("ZIP 文件不能为空")
    if material_type not in {"图片", "表情包", "头像包"}:
        raise ValueError("ZIP 导入仅支持图片、表情包和头像包素材")
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("ZIP 文件无法解析") from exc

    zip_image_max_bytes = min(get_settings().material_max_bytes, ZIP_IMAGE_MAX_BYTES)
    import_id = uuid4().hex
    source_filename = filename or "materials.zip"
    default_group = (title.strip() or Path(source_filename).stem or "素材包").strip()
    base_title = title.strip() or default_group
    stored_material_type = "图片" if material_type == "头像包" else material_type
    import_type = "avatar_pack" if material_type == "头像包" else ("sticker_pack" if material_type == "表情包" else "image_group")
    details: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    success_count = skipped_count = duplicate_count = oversize_count = failed_count = 0
    materials: list[Material] = []

    with archive:
        entries = [item for item in archive.infolist() if not item.is_dir()]
        for item in entries:
            entry_name = item.filename
            normalized_name = entry_name.replace("\\", "/").lstrip("/")
            parts = [part for part in normalized_name.split("/") if part]
            reason = ""
            payload = b""
            material_id: int | None = None
            if not parts or any(part == ".." for part in parts):
                reason = "路径不安全已跳过"
            elif parts[0] == "__MACOSX" or any(part.startswith("._") or part.startswith(".") for part in parts):
                reason = "系统目录已跳过"
            elif item.file_size > zip_image_max_bytes:
                reason = f"素材文件过大，最大 {zip_image_max_bytes} 字节"
                oversize_count += 1
            else:
                try:
                    payload = archive.read(item)
                except (KeyError, RuntimeError, zipfile.BadZipFile):
                    reason = "文件读取失败"
            if not reason and not payload:
                reason = "素材文件不能为空"
            if not reason and len(payload) > zip_image_max_bytes:
                reason = f"素材文件过大，最大 {zip_image_max_bytes} 字节"
                oversize_count += 1
            content_type = _zip_content_type(normalized_name, payload)
            if not reason and content_type not in {"image/png", "image/jpeg"}:
                reason = "素材文件类型不支持"
            digest = hashlib.sha256(payload).hexdigest() if payload else ""
            if not reason and digest in seen_hashes:
                reason = "重复文件已跳过"
                duplicate_count += 1
            if reason:
                skipped_count += 1
                details.append({"file_name": entry_name, "status": "skipped", "reason": reason, "material_id": None, "file_size": len(payload) if payload else int(item.file_size or 0)})
                continue
            seen_hashes.add(digest)
            try:
                material = _new_uploaded_material(
                    tenant_id=tenant_id,
                    title=_uploaded_material_title(base_title, Path(normalized_name).name, index=success_count + 1, multiple=True),
                    material_type=stored_material_type,
                    tags=tags,
                    caption=caption,
                    filename=Path(normalized_name).name,
                    content_type=content_type,
                    data=payload,
                    emoji_asset_kind="image_meme" if stored_material_type == "表情包" else "",
                )
                session.add(material)
                session.flush()
                material_id = material.id
                materials.append(material)
                success_count += 1
                details.append({"file_name": entry_name, "status": "created", "reason": "", "material_id": material_id, "file_size": len(payload)})
            except ValueError as exc:
                failed_count += 1
                details.append({"file_name": entry_name, "status": "failed", "reason": str(exc), "material_id": None, "file_size": len(payload)})

    job = MaterialImportJob(
        id=import_id,
        tenant_id=tenant_id,
        source_filename=source_filename,
        import_type=import_type,
        target_group_name=default_group,
        status="completed",
        total_count=len(details),
        success_count=success_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        duplicate_count=duplicate_count,
        oversize_count=oversize_count,
        item_details=details,
    )
    session.add(job)
    for material in materials:
        record_material_versions(session, material, actor=actor)
        audit(session, tenant_id=material.tenant_id, actor=actor, action="ZIP导入素材", target_type="material", target_id=str(material.id), detail=f"import_id={import_id}")
    audit(session, tenant_id=tenant_id, actor=actor, action="ZIP导入素材包", target_type="material_import", target_id=import_id, detail=f"success={success_count}; skipped={skipped_count}; failed={failed_count}")
    session.commit()
    session.refresh(job)
    return _material_import_result(job)


def _new_uploaded_material(
    *,
    tenant_id: int,
    title: str,
    material_type: str,
    tags: str,
    caption: str,
    filename: str,
    content_type: str,
    data: bytes,
    emoji_asset_kind: str,
) -> Material:
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
    return material


def _uploaded_material_title(base_title: str, filename: str, *, index: int, multiple: bool) -> str:
    if not multiple:
        return base_title
    file_stem = str(filename or "").rsplit(".", 1)[0].strip()
    suffix = file_stem or f"{index:02d}"
    if base_title == "素材":
        return suffix
    return f"{base_title}-{suffix}"


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
    return _attach_material_reference_summary(session, material)


def disable_material(session: Session, material_id: int, actor: str, *, reason: str = "") -> Material:
    material = session.get(Material, material_id)
    if not material:
        raise ValueError("material not found")
    material.review_status = "已禁用"
    summary = material_reference_summary(session, material.tenant_id, material.id)
    detail = f"reason={reason.strip() or '未填写'}; referenced_by={summary.total_count}"
    audit(session, tenant_id=material.tenant_id, actor=actor, action="禁用素材", target_type="material", target_id=str(material.id), detail=detail)
    session.commit()
    session.refresh(material)
    return _attach_material_reference_summary(session, material)


def restore_material(session: Session, material_id: int, actor: str) -> Material:
    material = session.get(Material, material_id)
    if not material:
        raise ValueError("material not found")
    material.review_status = "已审核"
    summary = material_reference_summary(session, material.tenant_id, material.id)
    audit(
        session,
        tenant_id=material.tenant_id,
        actor=actor,
        action="恢复素材",
        target_type="material",
        target_id=str(material.id),
        detail=f"referenced_by={summary.total_count}",
    )
    session.commit()
    session.refresh(material)
    return _attach_material_reference_summary(session, material)


USERNAME_PATTERN = re.compile(r"^@[A-Za-z0-9_]{1,64}$")
PEER_ID_PATTERN = re.compile(r"^-100\d{5,}$")
_UNSET = object()


def normalize_cache_channel_input(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if PEER_ID_PATTERN.match(raw):
        return raw
    if raw.startswith("@"):
        if not USERNAME_PATTERN.match(raw):
            raise ValueError("缓存频道 @username 格式无效")
        return raw
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if host not in {"t.me", "telegram.me"}:
        raise ValueError("缓存频道链接仅支持 t.me 或 telegram.me")
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        raise ValueError("缓存频道链接缺少频道标识")
    if parts[0] == "c":
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError("私有频道链接格式应为 t.me/c/<internal_id>/...")
        return f"-100{parts[1]}"
    username = f"@{parts[0]}"
    if not USERNAME_PATTERN.match(username):
        raise ValueError("缓存频道链接用户名格式无效")
    return username


def _get_material_cache_config(session: Session, tenant_id: int) -> MaterialCacheConfig | None:
    return session.scalar(select(MaterialCacheConfig).where(MaterialCacheConfig.tenant_id == tenant_id))


def _cache_channel_out(raw_input: str, normalized_peer: str, env_value: str, last_error: str = "") -> CacheChannelConfigOut:
    if normalized_peer:
        return CacheChannelConfigOut(raw_input=raw_input, normalized_peer=normalized_peer, source="saved", last_error=last_error)
    if env_value:
        return CacheChannelConfigOut(raw_input=env_value, normalized_peer=env_value, source="env", last_error=last_error)
    return CacheChannelConfigOut(raw_input="", normalized_peer="", source="empty", last_error=last_error)


def _cache_channel_no_account_error(normalized_peer: str) -> str:
    return f"已保存为 {normalized_peer}，但当前没有可用缓存账号；请先启用一个 TG 账号，并把该账号加入缓存频道。"


def _cache_channel_permission_error(normalized_peer: str) -> str:
    return f"已保存为 {normalized_peer}，但缓存账号暂不可访问或无发布权限；请将系统缓存账号加入该频道并授予发消息/发帖权限。"


def _cache_selected_account_unavailable_error(normalized_peer: str) -> str:
    return f"已保存为 {normalized_peer}，但指定缓存执行账号不可用；请重新登录该账号或改选在线账号。"


def _cache_selected_account_fallback_warning(normalized_peer: str) -> str:
    return f"已保存为 {normalized_peer}，但指定缓存执行账号暂不可访问或无发布权限；系统已验证备用缓存账号可用，请检查指定账号权限。"


def cache_candidate_accounts(session: Session, tenant_id: int, preferred_account_id: int | None = None) -> list[TgAccount]:
    accounts = list(
        session.scalars(
            select(TgAccount)
            .where(TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None), TgAccount.status == AccountStatus.ACTIVE.value)
            .order_by(TgAccount.health_score.desc(), TgAccount.id.asc())
        )
    )
    if not preferred_account_id:
        return accounts
    preferred = [account for account in accounts if account.id == preferred_account_id]
    if not preferred:
        return accounts
    return preferred + [account for account in accounts if account.id != preferred_account_id]


def _cache_account_out(account: TgAccount | None) -> CacheExecutionAccountOut | None:
    if not account:
        return None
    return CacheExecutionAccountOut(
        id=account.id,
        display_name=account.display_name,
        username=account.username,
        phone_masked=account.phone_masked,
        status=account.status,
        health_score=float(account.health_score or 0),
    )


def _validate_cache_account_id(session: Session, tenant_id: int, account_id: int | None) -> TgAccount | None:
    if not account_id:
        return None
    account = session.get(TgAccount, account_id)
    if not account or account.tenant_id != tenant_id or account.deleted_at is not None:
        raise ValueError("缓存执行账号不存在或不属于当前租户")
    return account


def _validate_cache_channel_access(session: Session, tenant_id: int, normalized_peer: str, preferred_account_id: int | None = None) -> str:
    if not normalized_peer:
        return ""
    accounts = cache_candidate_accounts(session, tenant_id)
    if not accounts:
        return _cache_channel_no_account_error(normalized_peer)
    if preferred_account_id and all(account.id != preferred_account_id for account in accounts):
        return _cache_selected_account_unavailable_error(normalized_peer)
    accounts = cache_candidate_accounts(session, tenant_id, preferred_account_id)
    from app.services.developer_apps import credentials_for_account

    preferred_failed = False
    for account in accounts:
        try:
            credentials = credentials_for_account(session, account)
            result = gateway.probe_target_capabilities(
                account.id,
                normalized_peer,
                "channel",
                account.session_ciphertext,
                credentials,
            )
        except Exception:  # noqa: BLE001 - try the next active account before surfacing a friendly message.
            if preferred_account_id and account.id == preferred_account_id:
                preferred_failed = True
            continue
        if getattr(result, "ok", False):
            return _cache_selected_account_fallback_warning(normalized_peer) if preferred_failed else ""
        if preferred_account_id and account.id == preferred_account_id:
            preferred_failed = True
    return _cache_channel_permission_error(normalized_peer)


def resolve_material_cache_peer_id(session: Session, tenant_id: int) -> str:
    config = _get_material_cache_config(session, tenant_id)
    if config and config.material_cache_peer_id:
        return config.material_cache_peer_id
    return get_settings().material_cache_peer_id


def resolve_material_cache_account_id(session: Session, tenant_id: int) -> int | None:
    config = _get_material_cache_config(session, tenant_id)
    return config.material_cache_account_id if config else None


def resolve_source_media_cache_peer_id(session: Session, tenant_id: int) -> str:
    config = _get_material_cache_config(session, tenant_id)
    if config and config.source_media_cache_peer_id:
        return config.source_media_cache_peer_id
    return get_settings().source_media_cache_peer_id


def get_material_cache_config(session: Session, tenant_id: int) -> MaterialCacheConfigOut:
    require_tenant(session, tenant_id)
    config = _get_material_cache_config(session, tenant_id)
    settings = get_settings()
    return MaterialCacheConfigOut(
        material_cache=_cache_channel_out(
            config.material_cache_input if config else "",
            config.material_cache_peer_id if config else "",
            settings.material_cache_peer_id,
            config.material_cache_last_error if config else "",
        ),
        source_media_cache=_cache_channel_out(
            config.source_media_cache_input if config else "",
            config.source_media_cache_peer_id if config else "",
            settings.source_media_cache_peer_id,
            config.source_media_cache_last_error if config else "",
        ),
        cache_account=_cache_account_out(session.get(TgAccount, config.material_cache_account_id) if config and config.material_cache_account_id else None),
        health=material_cache_health(session, tenant_id),
    )


def update_material_cache_config(
    session: Session,
    *,
    tenant_id: int,
    material_cache_input: str | None,
    source_media_cache_input: str | None,
    material_cache_account_id: int | None | object = _UNSET,
    actor: str,
) -> MaterialCacheConfigOut:
    require_tenant(session, tenant_id)
    config = _get_material_cache_config(session, tenant_id)
    if not config:
        config = MaterialCacheConfig(tenant_id=tenant_id)
        session.add(config)
    if material_cache_account_id is not _UNSET:
        account = _validate_cache_account_id(session, tenant_id, material_cache_account_id)
        config.material_cache_account_id = account.id if account else None
    if material_cache_input is not None:
        config.material_cache_input = material_cache_input.strip()
        config.material_cache_peer_id = normalize_cache_channel_input(config.material_cache_input)
        config.material_cache_last_error = _validate_cache_channel_access(session, tenant_id, config.material_cache_peer_id, config.material_cache_account_id)
    elif material_cache_account_id is not _UNSET and config.material_cache_peer_id:
        config.material_cache_last_error = _validate_cache_channel_access(session, tenant_id, config.material_cache_peer_id, config.material_cache_account_id)
    if source_media_cache_input is not None:
        config.source_media_cache_input = source_media_cache_input.strip()
        config.source_media_cache_peer_id = normalize_cache_channel_input(config.source_media_cache_input)
        config.source_media_cache_last_error = _validate_cache_channel_access(session, tenant_id, config.source_media_cache_peer_id, config.material_cache_account_id)
    config.updated_at = _now()
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="更新素材缓存频道配置",
        target_type="material_cache_config",
        target_id=str(tenant_id),
        detail=f"material={config.material_cache_peer_id or 'env'}; source_media={config.source_media_cache_peer_id or 'env'}; cache_account={config.material_cache_account_id or 'auto'}",
    )
    session.commit()
    return get_material_cache_config(session, tenant_id)


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
    return MaterialCacheHealthOut(
        material_cache_peer_configured=bool(resolve_material_cache_peer_id(session, tenant_id)),
        source_media_cache_peer_configured=bool(resolve_source_media_cache_peer_id(session, tenant_id)),
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
    "cache_candidate_accounts",
    "check_ai_provider",
    "create_ai_provider",
    "create_content_keyword_rule",
    "create_material",
    "create_material_group",
    "create_prompt_template",
    "create_uploaded_materials",
    "disable_material",
    "get_material",
    "get_scheduling_setting",
    "get_tenant_ai_setting",
    "list_ai_providers",
    "list_content_keyword_rules",
    "list_material_groups",
    "list_materials",
    "list_material_version_history",
    "list_prompt_templates",
    "list_usage_ledgers",
    "list_usage_summary",
    "material_references",
    "material_reference_summaries",
    "record_ai_usage",
    "resolve_material_cache_account_id",
    "refresh_material_cache",
    "require_ai_token_balance",
    "restore_material",
    "seed_ai_configuration",
    "deduct_ai_usage_tokens",
    "update_ai_provider",
    "update_content_keyword_rule",
    "update_material",
    "update_material_group",
    "update_prompt_template",
    "update_scheduling_setting",
    "update_tenant_ai_setting",
]
