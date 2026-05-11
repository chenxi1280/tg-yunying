from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AiProvider, AiProviderHealthStatus, TenantAiSetting
from app.services._common import ai_gateway
from app.services.ai_config import ai_provider_credentials


def _provider(session: Session, tenant_id: int, provider_id: int | None = None) -> tuple[AiProvider | None, TenantAiSetting | None]:
    setting = session.scalar(select(TenantAiSetting).where(TenantAiSetting.tenant_id == tenant_id))
    if not setting or not setting.ai_enabled:
        return None, setting
    if provider_id:
        provider = session.get(AiProvider, provider_id)
        if provider and provider.is_active and provider.health_status == AiProviderHealthStatus.HEALTHY.value:
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


def generate_contents(
    session: Session,
    tenant_id: int,
    *,
    topic: str,
    requirements: str,
    provider_id: int | None = None,
    count: int,
    purpose: str,
    target_label: str = "",
) -> tuple[list[str], int]:
    provider, setting = _provider(session, tenant_id, provider_id)
    if not provider or not setting:
        text = (requirements or topic or "这个话题挺值得继续聊聊。").strip()
        return ([text] * count)[:count], 0
    prompt = (
        f"请生成 {count} 条 Telegram {purpose}内容。\n"
        f"目标：{target_label}\n"
        f"主题：{topic}\n"
        f"要求：{requirements}\n"
        "每条都要自然、口语化、不要编号，不要暴露 AI 或运营任务。\n"
        '只输出 JSON：{"drafts":[{"persona":"自然用户","content":"内容","risk_level":"低"}]}'
    )
    result = ai_gateway.generate_drafts(
        ai_provider_credentials(provider),
        prompt,
        count=count,
        topic=topic or requirements,
        tone="自然、口语化、不同账号表达不重复",
        persona_set=["老用户", "新用户", "活跃成员", "路人"],
        temperature=setting.temperature,
        max_tokens=max(setting.max_tokens, 1024),
    )
    contents = [candidate.content.strip() for candidate in result.candidates if candidate.content.strip()]
    usage = getattr(result, "usage", None)
    tokens = int(getattr(usage, "total_tokens", 0) or 0)
    return contents[:count], tokens


def generate_group_messages(session: Session, tenant_id: int, config: dict, *, count: int, target_label: str, history: str = "") -> tuple[list[str], int]:
    requirements = "\n".join(part for part in [config.get("topic_hint") or "", history, config.get("system_prompt_override") or ""] if part)
    contents, tokens = generate_contents(
        session,
        tenant_id,
        topic=config.get("topic_hint") or "群聊日常活跃",
        requirements=requirements,
        provider_id=config.get("ai_provider_id"),
        count=count,
        purpose="群活跃续聊",
        target_label=target_label,
    )
    return _trim(contents, config.get("max_message_length")), tokens


def generate_channel_comments(session: Session, tenant_id: int, config: dict, *, count: int, message_content: str, target_label: str) -> tuple[list[str], int]:
    topic = config.get("topic_hint") or "频道评论"
    requirements = (
        f"频道消息：{message_content}\n"
        f"评论风格：{config.get('comment_style') or 'mixed'}\n"
        f"语言：{config.get('language') or 'zh-CN'}\n"
        f"{config.get('system_prompt_override') or ''}"
    )
    contents, tokens = generate_contents(
        session,
        tenant_id,
        topic=topic,
        requirements=requirements,
        provider_id=config.get("ai_provider_id"),
        count=count,
        purpose="频道评论",
        target_label=target_label,
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


__all__ = ["generate_channel_comments", "generate_group_messages", "rewrite_relay_content"]
