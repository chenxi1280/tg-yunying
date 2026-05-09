from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
import json
import random
import re
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import CurrentUser
from app.ai_gateway import AiUsage, mock_candidates
from app.models import (
    AccountStatus,
    AiDraft,
    AiProvider,
    AiProviderHealthStatus,
    Campaign,
    GroupAuthStatus,
    Material,
    MessageTask,
    PromptTemplate,
    TaskStatus,
    TenantAiSetting,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.task_queue import get_task_queue

from ._common import _as_utc, _now, audit, gateway, ai_gateway, require_tenant
from .ai_config import (
    ai_provider_credentials,
    deduct_ai_usage_tokens,
    get_scheduling_setting,
    get_tenant_ai_setting,
    record_ai_usage,
    require_ai_token_balance,
)
from .messages import dispatch_task
from .tenants import ensure_task_quota_available
from app.schemas import (
    CampaignCreate,
    CampaignRecommendAccountsRequest,
    GenerateDraftsRequest,
)


def recommend_campaign_accounts(session: Session, payload: CampaignRecommendAccountsRequest, tenant_id: int) -> list[dict]:
    require_tenant(session, tenant_id)
    group_ids = payload.target_group_ids
    if not group_ids:
        group_ids = list(session.scalars(select(TgGroup.id).where(TgGroup.tenant_id == tenant_id).order_by(TgGroup.id.asc()).limit(1)))
    recommendations: list[dict] = []
    for group_id in group_ids:
        group = session.get(TgGroup, group_id)
        if not group or group.tenant_id != tenant_id:
            continue
        links = list(
            session.scalars(
                select(TgGroupAccount)
                .where(TgGroupAccount.tenant_id == tenant_id, TgGroupAccount.group_id == group.id)
                .order_by(TgGroupAccount.id.asc())
            )
        )
        for link in links:
            account = session.get(TgAccount, link.account_id)
            if not account or account.deleted_at is not None:
                continue
            recent_failures = session.scalar(
                select(func.count(MessageTask.id)).where(
                    MessageTask.account_id == account.id,
                    MessageTask.status == TaskStatus.FAILED.value,
                )
            ) or 0
            can_send = (
                group.auth_status == GroupAuthStatus.AUTHORIZED.value
                and link.can_send
                and account.status == AccountStatus.ACTIVE.value
            )
            cooldown_active = False
            cooldown_until = None
            if link.last_sent_at:
                cooldown_until = link.last_sent_at + timedelta(seconds=group.account_cooldown_seconds)
                cooldown_active = _as_utc(cooldown_until) > _as_utc(_now())
            is_selectable = can_send and not cooldown_active
            recommended = is_selectable and account.health_score >= 70 and recent_failures < 3
            reason_parts = []
            if account.status != AccountStatus.ACTIVE.value:
                reason_parts.append(account.status)
            if group.auth_status != GroupAuthStatus.AUTHORIZED.value:
                reason_parts.append(group.auth_status)
            if not link.can_send:
                reason_parts.append("账号不可发言")
            if cooldown_active:
                reason_parts.append("账号冷却中")
            if recent_failures >= 3:
                reason_parts.append("近期失败较多")
            if not reason_parts:
                reason_parts.append("健康度较高，具备群发言权限")
            unavailable_reason = None if is_selectable else "、".join(reason_parts)
            recommendations.append(
                {
                    "group_id": group.id,
                    "group_title": group.title,
                    "account_id": account.id,
                    "account_name": account.display_name,
                    "username": account.username,
                    "health_score": account.health_score,
                    "can_send": can_send,
                    "is_selectable": is_selectable,
                    "unavailable_reason": unavailable_reason,
                    "cooldown_until": cooldown_until,
                    "recommended": recommended,
                    "reason": "、".join(reason_parts),
                }
            )
    return recommendations


def validate_selected_accounts_by_group(
    session: Session,
    tenant_id: int,
    target_group_ids: list[int],
    selected_accounts: dict[str, list[int]],
) -> dict[str, list[int]]:
    normalized: dict[str, list[int]] = {}
    valid_group_ids = set(target_group_ids)
    require_complete_selection = bool(selected_accounts)
    if require_complete_selection:
        missing_group_ids = [group_id for group_id in target_group_ids if not selected_accounts.get(str(group_id))]
        if missing_group_ids:
            raise ValueError(f"target groups missing selected accounts: {missing_group_ids}")
    for raw_group_id, account_ids in selected_accounts.items():
        group_id = int(raw_group_id)
        if group_id not in valid_group_ids:
            raise ValueError("selected account group not in targets")
        group = session.get(TgGroup, group_id)
        if not group or group.tenant_id != tenant_id:
            raise ValueError("target group not found")
        if group.auth_status != GroupAuthStatus.AUTHORIZED.value:
            raise ValueError("target group is not authorized for operation")
        clean_ids: list[int] = []
        for account_id in dict.fromkeys(account_ids):
            account = session.get(TgAccount, account_id)
            link = session.scalar(
                select(TgGroupAccount).where(
                    TgGroupAccount.tenant_id == tenant_id,
                    TgGroupAccount.group_id == group_id,
                    TgGroupAccount.account_id == account_id,
                )
            )
            if not account or account.deleted_at is not None or account.tenant_id != tenant_id:
                raise ValueError("selected account not found")
            if not link or not link.can_send:
                raise ValueError("selected account cannot send in target group")
            if account.status != AccountStatus.ACTIVE.value:
                raise ValueError("selected account is not online")
            clean_ids.append(account.id)
        normalized[str(group_id)] = clean_ids
    return normalized


def create_campaign(session: Session, payload: CampaignCreate, actor: str = "普通用户") -> Campaign:
    require_tenant(session, payload.tenant_id)
    group = session.get(TgGroup, payload.group_id)
    if not group or group.tenant_id != payload.tenant_id:
        raise ValueError("group not found")
    data = payload.model_dump()
    execution_mode = data.get("execution_mode") or "manual_draft"
    if execution_mode not in {"manual_draft", "ai_activity", "mirror_forward"}:
        raise ValueError("unsupported campaign execution mode")
    if execution_mode in {"ai_activity", "mirror_forward"} and not data.get("ends_at"):
        raise ValueError("continuous campaign requires ends_at")
    if data.get("participation_min_ratio", 0.6) > data.get("participation_max_ratio", 1.0):
        raise ValueError("participation_min_ratio cannot exceed participation_max_ratio")
    target_group_ids = data.pop("target_group_ids", []) or [payload.group_id]
    source_group_ids = data.pop("source_group_ids", []) or []
    selected_accounts = data.pop("selected_account_ids_by_group", {}) or {}
    all_groups = session.scalars(select(TgGroup).where(TgGroup.id.in_(list(dict.fromkeys(target_group_ids))), TgGroup.tenant_id == payload.tenant_id)).all()
    valid_group_ids = [group.id for group in all_groups]
    if not valid_group_ids:
        raise ValueError("target groups not found")
    source_groups = []
    if source_group_ids:
        source_groups = list(
            session.scalars(
                select(TgGroup).where(
                    TgGroup.id.in_(list(dict.fromkeys(source_group_ids))),
                    TgGroup.tenant_id == payload.tenant_id,
                )
            )
        )
    valid_source_group_ids = [group.id for group in source_groups]
    if execution_mode == "mirror_forward" and not valid_source_group_ids:
        raise ValueError("mirror forwarding requires source groups")
    selected_accounts = validate_selected_accounts_by_group(session, payload.tenant_id, valid_group_ids, selected_accounts)
    if execution_mode in {"ai_activity", "mirror_forward"}:
        missing_group_ids = [group_id for group_id in valid_group_ids if not selected_accounts.get(str(group_id))]
        if missing_group_ids:
            raise ValueError(f"target groups missing selected accounts: {missing_group_ids}")
    data["group_id"] = valid_group_ids[0]
    data["target_group_ids"] = ",".join(str(group_id) for group_id in valid_group_ids)
    data["source_group_ids"] = ",".join(str(group_id) for group_id in valid_source_group_ids)
    data["selected_account_ids_by_group"] = json.dumps({str(key): value for key, value in selected_accounts.items()}, ensure_ascii=False)
    if execution_mode == "ai_activity" and data.get("max_ai_tokens") is None:
        data["max_ai_tokens"] = 100000
    if execution_mode == "mirror_forward":
        data["max_ai_tokens"] = None
    campaign_status = TaskStatus.DRAFT.value if execution_mode == "manual_draft" else TaskStatus.RUNNING.value
    campaign = Campaign(**data, status=campaign_status)
    session.add(campaign)
    session.flush()
    audit(session, tenant_id=campaign.tenant_id, actor=actor, action="创建活跃任务", target_type="campaign", target_id=str(campaign.id))
    session.commit()
    session.refresh(campaign)
    return campaign


def parse_id_list(raw: str | None) -> list[int]:
    if not raw:
        return []
    return [int(value) for value in re.findall(r"\d+", raw)]


def campaign_materials(session: Session, campaign: Campaign) -> list[Material]:
    ids = parse_id_list(campaign.material_ids)
    if not ids:
        return []
    return list(
        session.scalars(
            select(Material).where(
                Material.tenant_id == campaign.tenant_id,
                Material.id.in_(ids),
                Material.review_status == "已审核",
            )
        )
    )


@dataclass(frozen=True)
class PromptDecision:
    use_ai: bool
    fallback_to_mock: bool
    template_type: str
    generation_source: str
    decision_template: PromptTemplate | None
    reason: str


def pick_prompt_template(session: Session, campaign: Campaign, template_type: str = "群活跃草稿") -> PromptTemplate:
    if campaign.prompt_template_id:
        template = session.get(PromptTemplate, campaign.prompt_template_id)
        if template and template.is_active and (template.tenant_id is None or template.tenant_id == campaign.tenant_id):
            return template
    template = session.scalar(
        select(PromptTemplate)
        .where(
            PromptTemplate.is_active.is_(True),
            PromptTemplate.template_type == template_type,
            or_(PromptTemplate.tenant_id == campaign.tenant_id, PromptTemplate.tenant_id.is_(None)),
        )
        .order_by(PromptTemplate.tenant_id.is_(None), PromptTemplate.id.asc())
    )
    if not template and template_type != "群活跃草稿":
        template = session.scalar(
            select(PromptTemplate)
            .where(
                PromptTemplate.is_active.is_(True),
                PromptTemplate.template_type == "群活跃草稿",
                or_(PromptTemplate.tenant_id == campaign.tenant_id, PromptTemplate.tenant_id.is_(None)),
            )
            .order_by(PromptTemplate.tenant_id.is_(None), PromptTemplate.id.asc())
        )
    if not template:
        raise ValueError("prompt template not found")
    return template


def pick_system_decision_template(session: Session, tenant_id: int) -> PromptTemplate | None:
    return session.scalar(
        select(PromptTemplate)
        .where(
            PromptTemplate.is_active.is_(True),
            PromptTemplate.template_type == "系统决策提示词",
            or_(PromptTemplate.tenant_id == tenant_id, PromptTemplate.tenant_id.is_(None)),
        )
        .order_by(PromptTemplate.tenant_id.is_(None), PromptTemplate.id.asc())
    )


def resolve_prompt_decision(
    session: Session,
    *,
    campaign: Campaign,
    group: TgGroup,
    payload: GenerateDraftsRequest,
    tenant_setting: TenantAiSetting,
    materials: list[Material],
) -> PromptDecision:
    decision_template = pick_system_decision_template(session, campaign.tenant_id)
    has_materials = bool(materials)
    raw = " ".join([campaign.campaign_type, campaign.topic, group.topic_direction, decision_template.content if decision_template else ""])
    if has_materials:
        template_type = "素材配文"
    elif "对话" in raw or "多账号" in raw or "多轮" in raw:
        template_type = "多账号对话脚本"
    else:
        template_type = "群活跃草稿"

    if not payload.use_ai:
        return PromptDecision(False, payload.fallback_to_mock, template_type, "system_skipped", decision_template, "本次请求关闭 AI")
    if not tenant_setting.ai_enabled:
        return PromptDecision(False, tenant_setting.fallback_to_mock, template_type, "system_skipped", decision_template, "客户 AI 配置关闭")
    return PromptDecision(
        True,
        payload.fallback_to_mock and tenant_setting.fallback_to_mock,
        template_type,
        "system_decision",
        decision_template,
        "系统决策提示词允许调用 AI",
    )


def pick_ai_provider(session: Session, campaign: Campaign, setting: TenantAiSetting) -> AiProvider | None:
    provider_id = campaign.ai_provider_id or setting.default_provider_id
    if provider_id:
        provider = session.get(AiProvider, provider_id)
        if provider and provider.is_active and provider.health_status == AiProviderHealthStatus.HEALTHY.value:
            return provider
    return session.scalar(
        select(AiProvider)
        .where(AiProvider.is_active.is_(True), AiProvider.health_status == AiProviderHealthStatus.HEALTHY.value)
        .order_by(AiProvider.id.asc())
    )


def render_prompt(
    template: PromptTemplate,
    *,
    campaign: Campaign,
    group: TgGroup,
    payload: GenerateDraftsRequest,
    materials: list[Material],
    selected_accounts: list[TgAccount],
    listener_account: TgAccount | None = None,
) -> str:
    material_text = "\n".join(f"#{item.id} {item.material_type} {item.title}: {item.content[:180]}" for item in materials) or "无"
    labels = [chr(ord("A") + index) for index in range(len(selected_accounts))]
    account_text = "\n".join(
        f"{labels[index]}账号: #{account.id} {account.display_name} @{account.username or '-'} 健康分 {account.health_score:.0f}"
        for index, account in enumerate(selected_accounts)
    ) or "未指定，由系统自动分配"
    listener_text = (
        f"监听账号: #{listener_account.id} {listener_account.display_name} @{listener_account.username or '-'}"
        if listener_account
        else "监听账号: 未指定"
    )
    context_items = payload.conversation_context[-20:]
    context_text = "\n".join(
        f"{item.sent_at or '-'} {item.sender_name}: {item.content[:500]}"
        for item in context_items
        if item.content.strip()
    ) or "暂无真人上下文"
    variables = {
        "count": str(payload.count),
        "group_title": group.title,
        "topic": campaign.topic,
        "topic_direction": group.topic_direction,
        "tone": payload.tone,
        "persona_set": "、".join(payload.persona_set),
        "banned_words": group.banned_words or "无",
        "materials": material_text,
        "selected_accounts": account_text,
        "listener_account": listener_text,
        "conversation_context": context_text,
    }
    rendered = template.content
    for key, value in variables.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return (
        rendered
        + "\n\n输出要求：请只返回 JSON，格式为 {\"drafts\":[...] }。"
        + "每条 drafts 必须包含 sequence_index、persona、content、risk_level，"
        + "可选 suggested_account_id、reply_to_sequence_index、material_id。"
        + "请按所选账号顺序从 A 账号开始轮流生成，suggested_account_id 必须优先使用对应账号 id。"
        + "如果有真人上下文，要接住真人最新消息继续聊；不要暴露运营、脚本、AI、监听等内部信息。"
        + f"\n\n所选账号：\n{account_text}\n{listener_text}\n真人上下文：\n{context_text}"
    )


def prompt_template_label(template: PromptTemplate, decision: PromptDecision) -> str:
    business_label = f"{template.name} v{template.version}"
    if not decision.decision_template:
        return business_label[:120]
    decision_label = f"{decision.decision_template.name} v{decision.decision_template.version}"
    return f"{business_label} / {decision_label}"[:120]


def generate_drafts(
    session: Session,
    campaign_id: int,
    payload: GenerateDraftsRequest,
    current_user: CurrentUser,
    *,
    auto_commit: bool = True,
) -> list[AiDraft]:
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise ValueError("campaign not found")

    campaign.status = TaskStatus.PENDING_REVIEW.value
    target_group_id = payload.target_group_id or campaign.group_id
    if target_group_id not in campaign_target_group_ids(campaign):
        raise ValueError("target group not in campaign targets")
    group = session.get(TgGroup, target_group_id)
    if not group:
        raise ValueError("group not found")
    tenant_setting = get_tenant_ai_setting(session, campaign.tenant_id)
    materials = campaign_materials(session, campaign)
    material_ids = [material.id for material in materials]
    selected_accounts = load_selected_accounts_for_group(session, campaign, group.id)
    selected_account_ids = [account.id for account in selected_accounts]
    if payload.selected_account_ids_by_group:
        override_ids = payload.selected_account_ids_by_group.get(str(group.id), [])
        if override_ids:
            selected_account_ids = [account_id for account_id in override_ids if account_id in set(selected_account_ids or override_ids)]
            selected_accounts = list(
                session.scalars(
                    select(TgAccount)
                    .where(TgAccount.tenant_id == campaign.tenant_id, TgAccount.id.in_(selected_account_ids), TgAccount.deleted_at.is_(None))
                    .order_by(TgAccount.id.asc())
                )
            )
    listener_account = None
    if payload.listener_account_id is not None:
        listener_account = session.get(TgAccount, payload.listener_account_id)
        listener_link = session.scalar(
            select(TgGroupAccount).where(
                TgGroupAccount.tenant_id == campaign.tenant_id,
                TgGroupAccount.group_id == group.id,
                TgGroupAccount.account_id == payload.listener_account_id,
            )
        )
        if not listener_account or listener_account.deleted_at is not None or listener_account.tenant_id != campaign.tenant_id or not listener_link:
            raise ValueError("listener account cannot access target group")
    decision = resolve_prompt_decision(
        session,
        campaign=campaign,
        group=group,
        payload=payload,
        tenant_setting=tenant_setting,
        materials=materials,
    )
    template = pick_prompt_template(session, campaign, decision.template_type)
    provider = pick_ai_provider(session, campaign, tenant_setting)
    if decision.use_ai and provider and not provider.base_url.startswith("mock://"):
        require_ai_token_balance(session, current_user)
    prompt = render_prompt(
        template,
        campaign=campaign,
        group=group,
        payload=payload,
        materials=materials,
        selected_accounts=selected_accounts,
        listener_account=listener_account,
    )
    generation_source = decision.generation_source
    generation_error = ""
    provider_name = "系统决策" if not decision.use_ai else "Mock"
    model_name = decision.template_type if not decision.use_ai else "mock"
    usage = AiUsage()
    try:
        if not decision.use_ai:
            candidates = mock_candidates(payload.count, campaign.topic, payload.tone, payload.persona_set, material_ids, selected_account_ids)
        elif not provider:
            raise RuntimeError("没有健康 AI 供应商")
        else:
            provider_name = provider.provider_name
            model_name = provider.model_name
            result = ai_gateway.generate_drafts(
                ai_provider_credentials(provider),
                prompt,
                count=payload.count,
                topic=campaign.topic,
                tone=payload.tone,
                persona_set=payload.persona_set,
                temperature=tenant_setting.temperature,
                max_tokens=tenant_setting.max_tokens,
                material_ids=material_ids,
                selected_account_ids=selected_account_ids,
            )
            candidates = result.candidates
            usage = result.usage
            generation_source = provider.provider_type
    except Exception as exc:  # noqa: BLE001 - operator-facing fallback detail.
        generation_error = str(exc)
        record_ai_usage(
            session,
            current_user=current_user,
            campaign=campaign,
            group=group,
            template=template,
            provider=provider,
            provider_name=provider_name,
            model_name=model_name,
            usage=usage,
            request_status="failed",
            error_detail=generation_error,
        )
        if not decision.fallback_to_mock:
            audit(session, tenant_id=campaign.tenant_id, actor="AI服务", action="生成AI草稿失败", target_type="campaign", target_id=str(campaign.id), detail=generation_error)
            if auto_commit:
                session.commit()
            raise ValueError(generation_error) from exc
        candidates = mock_candidates(payload.count, campaign.topic, payload.tone, payload.persona_set, material_ids, selected_account_ids)
        generation_source = "mock_fallback"
    drafts: list[AiDraft] = []
    sequence_to_draft: dict[int, AiDraft] = {}
    for index, candidate in enumerate(candidates[: payload.count], start=1):
        sequence_index = candidate.sequence_index or index
        suggested_account_id = candidate.suggested_account_id
        if suggested_account_id not in selected_account_ids:
            suggested_account_id = selected_account_ids[(index - 1) % len(selected_account_ids)] if selected_account_ids else None
        draft = AiDraft(
            tenant_id=campaign.tenant_id,
            campaign_id=campaign.id,
            group_id=group.id,
            persona=candidate.persona,
            content=candidate.content,
            risk_level=candidate.risk_level,
            provider_name=provider_name,
            model_name=model_name,
            prompt_template_name=prompt_template_label(template, decision),
            material_id=candidate.material_id,
            suggested_account_id=suggested_account_id,
            sequence_index=sequence_index,
            generation_source=generation_source,
            generation_error=generation_error,
        )
        session.add(draft)
        drafts.append(draft)
        sequence_to_draft[sequence_index] = draft
    session.flush()
    for draft, candidate in zip(drafts, candidates[: payload.count], strict=False):
        reply_sequence = candidate.reply_to_sequence_index
        if reply_sequence and reply_sequence in sequence_to_draft:
            draft.reply_to_draft_id = sequence_to_draft[reply_sequence].id

    audit(
        session,
        tenant_id=campaign.tenant_id,
        actor="AI服务",
        action="生成AI草稿",
        target_type="campaign",
        target_id=str(campaign.id),
        detail=f"count={len(drafts)}; source={generation_source}; decision={decision.reason}; template_type={decision.template_type}",
    )
    usage_ledger = record_ai_usage(
        session,
        current_user=current_user,
        campaign=campaign,
        group=group,
        template=template,
        provider=provider if generation_source != "mock_fallback" else None,
        provider_name=provider_name,
        model_name=model_name,
        usage=usage if generation_source != "mock_fallback" else AiUsage(),
        request_status="success",
        error_detail=generation_error,
    )
    if generation_source != "mock_fallback" and usage.total_tokens > 0:
        session.flush()
        deduct_ai_usage_tokens(session, current_user, usage_ledger)
    if auto_commit:
        session.commit()
        for draft in drafts:
            session.refresh(draft)
    return drafts


def parse_time_window(raw: str | None) -> tuple[time, time] | None:
    if not raw:
        return None
    match = re.search(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", raw)
    if not match:
        return None
    start_hour, start_minute, end_hour, end_minute = [int(value) for value in match.groups()]
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23 and 0 <= start_minute <= 59 and 0 <= end_minute <= 59):
        return None
    return time(start_hour, start_minute), time(end_hour, end_minute)


def align_to_send_window(value: datetime, raw_window: str | None) -> datetime:
    parsed = parse_time_window(raw_window)
    if not parsed:
        return value
    start, end = parsed
    local_time = value.time()
    if start <= end:
        if local_time < start:
            return value.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        if local_time > end:
            next_day = value + timedelta(days=1)
            return next_day.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        return value
    if local_time > end and local_time < start:
        return value.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    return value


def campaign_schedule_config(session: Session, campaign: Campaign) -> dict:
    tenant_setting = get_scheduling_setting(session, campaign.tenant_id)
    platform_setting = get_scheduling_setting(session, None)
    group = session.get(TgGroup, campaign.group_id)
    jitter_min = campaign.jitter_min_seconds if campaign.jitter_min_seconds is not None else tenant_setting.jitter_min_seconds
    jitter_max = campaign.jitter_max_seconds if campaign.jitter_max_seconds is not None else tenant_setting.jitter_max_seconds
    batch_interval = campaign.batch_interval_seconds if campaign.batch_interval_seconds is not None else tenant_setting.batch_interval_seconds
    respect_window = campaign.respect_send_window if campaign.respect_send_window is not None else tenant_setting.respect_send_window
    if jitter_min is None:
        jitter_min = platform_setting.jitter_min_seconds
    if jitter_max is None:
        jitter_max = platform_setting.jitter_max_seconds
    if batch_interval is None:
        batch_interval = platform_setting.batch_interval_seconds
    if respect_window is None:
        respect_window = platform_setting.respect_send_window
    jitter_min = max(int(jitter_min), 0)
    jitter_max = max(int(jitter_max), jitter_min)
    return {
        "jitter_min": jitter_min,
        "jitter_max": jitter_max,
        "batch_interval": max(int(batch_interval), 0),
        "respect_window": bool(respect_window),
        "send_window": campaign.send_window or (group.active_window if group else None),
    }


def scheduled_at_for_campaign(session: Session, campaign: Campaign, index: int) -> tuple[datetime, int]:
    config = campaign_schedule_config(session, campaign)
    jitter = random.randint(config["jitter_min"], config["jitter_max"]) if config["jitter_max"] else 0
    delay = index * config["batch_interval"] + jitter
    scheduled_at = _now() + timedelta(seconds=delay)
    if config["respect_window"]:
        aligned_at = align_to_send_window(scheduled_at, config["send_window"])
        if aligned_at != scheduled_at:
            scheduled_at = aligned_at
            delay = max(0, int((scheduled_at - _now()).total_seconds()))
    return scheduled_at, delay


def campaign_target_group_ids(campaign: Campaign) -> list[int]:
    ids = parse_id_list(campaign.target_group_ids)
    return ids or [campaign.group_id]


def campaign_selected_accounts(campaign: Campaign) -> dict[str, list[int]]:
    if not campaign.selected_account_ids_by_group:
        return {}
    try:
        raw = json.loads(campaign.selected_account_ids_by_group)
    except json.JSONDecodeError:
        return {}
    return {str(key): [int(value) for value in values] for key, values in raw.items() if isinstance(values, list)}


def selected_account_ids_for_group(campaign: Campaign, group_id: int) -> list[int]:
    selected = campaign_selected_accounts(campaign)
    return selected.get(str(group_id), [])


def load_selected_accounts_for_group(session: Session, campaign: Campaign, group_id: int) -> list[TgAccount]:
    account_ids = selected_account_ids_for_group(campaign, group_id)
    if not account_ids:
        return []
    rows = list(
        session.scalars(
            select(TgAccount)
            .where(TgAccount.tenant_id == campaign.tenant_id, TgAccount.id.in_(account_ids), TgAccount.deleted_at.is_(None))
            .order_by(TgAccount.id.asc())
        )
    )
    by_id = {account.id: account for account in rows}
    return [by_id[account_id] for account_id in account_ids if account_id in by_id]


def task_media_from_draft(session: Session, draft: AiDraft) -> tuple[str, int | None]:
    if not draft.material_id:
        return "文本", None
    material = session.get(Material, draft.material_id)
    if not material or material.tenant_id != draft.tenant_id:
        return "文本", None
    if material.material_type in {"图片", "表情包", "文件", "链接", "组合消息"}:
        return material.material_type, material.id
    return "文本", material.id


def build_message_task_from_draft(session: Session, draft: AiDraft, actor: str, index: int = 0, target_group_id: int | None = None) -> MessageTask:
    campaign = session.get(Campaign, draft.campaign_id)
    if not campaign:
        raise ValueError("campaign not found")
    scheduled_at, planned_delay = scheduled_at_for_campaign(session, campaign, index)
    message_type, material_id = task_media_from_draft(session, draft)
    group_id = target_group_id or draft.group_id
    selected_ids = selected_account_ids_for_group(campaign, group_id)
    preferred_account_id = draft.suggested_account_id
    if preferred_account_id not in selected_ids:
        preferred_account_id = selected_ids[index % len(selected_ids)] if selected_ids else None
    task = MessageTask(
        tenant_id=draft.tenant_id,
        campaign_id=draft.campaign_id,
        group_id=group_id,
        draft_id=draft.id,
        content=draft.content,
        message_type=message_type,
        material_id=material_id,
        target_type="group",
        preferred_account_id=preferred_account_id,
        planned_delay_seconds=planned_delay,
        scheduled_at=scheduled_at,
        status=TaskStatus.QUEUED.value,
        idempotency_key=f"draft:{draft.id}:{uuid4().hex[:12]}",
    )
    session.add(task)
    session.flush()
    get_task_queue().enqueue(task.id)
    audit(session, tenant_id=draft.tenant_id, actor=actor, action="审核通过AI草稿", target_type="ai_draft", target_id=str(draft.id), detail=f"message_task={task.id}; delay={planned_delay}s")
    return task


def approve_draft(session: Session, draft_id: int, actor: str) -> MessageTask:
    draft = session.get(AiDraft, draft_id)
    if not draft:
        raise ValueError("draft not found")

    draft.status = TaskStatus.APPROVED.value
    campaign = session.get(Campaign, draft.campaign_id)
    target_group_ids = campaign_target_group_ids(campaign) if campaign else [draft.group_id]
    ensure_task_quota_available(session, draft.tenant_id, len(target_group_ids))
    tasks = [build_message_task_from_draft(session, draft, actor, index, group_id) for index, group_id in enumerate(target_group_ids)]
    if campaign:
        campaign.status = TaskStatus.QUEUED.value
    session.commit()
    task = tasks[0]
    session.refresh(task)
    return task


def approve_all_drafts(session: Session, campaign_id: int, actor: str) -> list[MessageTask]:
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise ValueError("campaign not found")
    drafts = list(
        session.scalars(
            select(AiDraft).where(
                AiDraft.campaign_id == campaign.id,
                AiDraft.status == TaskStatus.PENDING_REVIEW.value,
            ).order_by(AiDraft.sequence_index.asc(), AiDraft.id.asc())
        )
    )
    tasks: list[MessageTask] = []
    target_group_ids = campaign_target_group_ids(campaign)
    ensure_task_quota_available(session, campaign.tenant_id, len(drafts) * len(target_group_ids))
    task_index = 0
    for draft in drafts:
        draft.status = TaskStatus.APPROVED.value
        for group_id in target_group_ids:
            task = build_message_task_from_draft(session, draft, actor, task_index, group_id)
            tasks.append(task)
            task_index += 1
    campaign.status = TaskStatus.QUEUED.value if tasks else campaign.status
    audit(session, tenant_id=campaign.tenant_id, actor=actor, action="批量审核AI草稿", target_type="campaign", target_id=str(campaign.id), detail=f"tasks={len(tasks)}")
    session.commit()
    for task in tasks:
        session.refresh(task)
    return tasks


def campaign_detail(session: Session, campaign_id: int) -> dict:
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise ValueError("campaign not found")
    target_groups = list(
        session.scalars(
            select(TgGroup)
            .where(TgGroup.tenant_id == campaign.tenant_id, TgGroup.id.in_(campaign_target_group_ids(campaign)))
            .order_by(TgGroup.id.asc())
        )
    )
    selected: dict[str, list[TgAccount]] = {}
    for group_id, account_ids in campaign_selected_accounts(campaign).items():
        if not account_ids:
            selected[group_id] = []
            continue
        rows = list(
            session.scalars(
                select(TgAccount)
                .where(TgAccount.tenant_id == campaign.tenant_id, TgAccount.id.in_(account_ids), TgAccount.deleted_at.is_(None))
                .order_by(TgAccount.id.asc())
            )
        )
        by_id = {account.id: account for account in rows}
        selected[group_id] = [by_id[account_id] for account_id in account_ids if account_id in by_id]
    drafts = list(
        session.scalars(
            select(AiDraft)
            .where(AiDraft.tenant_id == campaign.tenant_id, AiDraft.campaign_id == campaign.id)
            .order_by(AiDraft.sequence_index.asc(), AiDraft.id.asc())
        )
    )
    tasks = list(
        session.scalars(
            select(MessageTask)
            .where(MessageTask.tenant_id == campaign.tenant_id, MessageTask.campaign_id == campaign.id)
            .order_by(MessageTask.scheduled_at.asc(), MessageTask.id.asc())
        )
    )
    return {
        "campaign": campaign,
        "target_groups": target_groups,
        "selected_accounts_by_group": selected,
        "drafts": drafts,
        "message_tasks": tasks,
        "stats": {
            "target_groups": len(target_groups),
            "source_groups": len(parse_id_list(campaign.source_group_ids)),
            "selected_accounts": sum(len(items) for items in selected.values()),
            "drafts": len(drafts),
            "filtered": campaign.filtered_count,
            "used_ai_tokens": campaign.used_ai_tokens,
            "queued": sum(1 for task in tasks if task.status == TaskStatus.QUEUED.value),
            "sent": sum(1 for task in tasks if task.status == TaskStatus.SENT.value),
            "failed": sum(1 for task in tasks if task.status == TaskStatus.FAILED.value),
        },
    }


def list_ai_drafts_for_tenant(session: Session, tenant_id: int) -> list[AiDraft]:
    return list(
        session.scalars(
            select(AiDraft)
            .where(AiDraft.tenant_id == tenant_id)
            .order_by(AiDraft.id.desc())
        )
    )


def filter_campaigns(session: Session, tenant_id: int, page: int, page_size: int, search: str | None, status: str | None) -> list[Campaign]:
    require_tenant(session, tenant_id)
    stmt = select(Campaign).where(Campaign.tenant_id == tenant_id)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(Campaign.title.like(like), Campaign.topic.like(like)))
    if status:
        stmt = stmt.where(Campaign.status == status)
    return list(session.scalars(stmt.order_by(Campaign.id.desc()).offset((page - 1) * page_size).limit(page_size)))


def update_ai_draft(session: Session, draft_id: int, payload, actor: str) -> AiDraft:
    draft = session.get(AiDraft, draft_id)
    if not draft:
        raise ValueError("draft not found")
    if draft.status == TaskStatus.APPROVED.value:
        raise ValueError("approved draft cannot be edited")
    data = payload.model_dump(exclude_unset=True)
    if "content" in data:
        content = (data["content"] or "").strip()
        if not content:
            raise ValueError("draft content is required")
        draft.content = content
    if "risk_level" in data and data["risk_level"]:
        draft.risk_level = data["risk_level"]
    if "suggested_account_id" in data:
        account_id = data["suggested_account_id"]
        if account_id is not None:
            account = session.get(TgAccount, account_id)
            if not account or account.deleted_at is not None or account.tenant_id != draft.tenant_id:
                raise ValueError("suggested account not found")
            campaign = session.get(Campaign, draft.campaign_id)
            selected_ids = selected_account_ids_for_group(campaign, draft.group_id) if campaign else []
            if selected_ids and account.id not in selected_ids:
                raise ValueError("suggested account is not in selected account pool")
        draft.suggested_account_id = account_id
    audit(session, tenant_id=draft.tenant_id, actor=actor, action="编辑AI草稿", target_type="ai_draft", target_id=str(draft.id))
    session.commit()
    session.refresh(draft)
    return draft


def reject_ai_draft(session: Session, draft_id: int, actor: str) -> AiDraft:
    draft = session.get(AiDraft, draft_id)
    if not draft:
        raise ValueError("draft not found")
    if draft.status == TaskStatus.APPROVED.value:
        raise ValueError("approved draft cannot be rejected")
    draft.status = TaskStatus.REJECTED.value
    audit(session, tenant_id=draft.tenant_id, actor=actor, action="驳回AI草稿", target_type="ai_draft", target_id=str(draft.id))
    session.commit()
    session.refresh(draft)
    return draft


__all__ = [
    "recommend_campaign_accounts",
    "validate_selected_accounts_by_group",
    "create_campaign",
    "campaign_detail",
    "filter_campaigns",
    "generate_drafts",
    "list_ai_drafts_for_tenant",
    "approve_draft",
    "approve_all_drafts",
    "update_ai_draft",
    "reject_ai_draft",
    "campaign_selected_accounts",
]
