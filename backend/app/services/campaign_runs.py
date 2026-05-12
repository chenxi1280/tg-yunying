from __future__ import annotations

from collections import Counter
from datetime import timedelta
import random
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountStatus,
    AiDraft,
    AiUsageLedger,
    Campaign,
    CampaignProcessedMessage,
    GroupAuthStatus,
    GroupContextMessage,
    TaskStatus,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.schemas import GenerateDraftsRequest

from ._common import SUBSCRIPTION_INACTIVE_DETAIL, _now, ai_gateway, audit, require_system_user_core_features
from .ai_config import ai_provider_credentials
from .ai_config import get_tenant_ai_setting
from .campaigns import (
    build_message_task_from_draft,
    campaign_target_group_ids,
    generate_drafts,
    load_selected_accounts_for_group,
    parse_id_list,
    pick_ai_provider,
)
from .content_filters import filter_outbound_content
from .group_listeners import collect_group_context, recent_context_messages
from .tenants import ensure_task_quota_available


CONTINUOUS_MODES = {"ai_activity", "mirror_forward"}
ACTIVE_CONTINUOUS_STATUSES = {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value}
CAMPAIGN_BACKOFF_CAP_SECONDS = 3600


def build_participation_plan(
    account_ids: list[int],
    message_count: int,
    *,
    ratio: float,
    max_messages_per_account: int,
    rng: random.Random | None = None,
) -> list[int]:
    if not account_ids or message_count <= 0:
        return []
    picker = rng or random
    max_per = max(1, int(max_messages_per_account))
    ratio = min(1.0, max(0.0, float(ratio)))
    wanted_participants = max(1, round(len(account_ids) * ratio))
    required_participants = (message_count + max_per - 1) // max_per
    participant_count = min(len(account_ids), max(wanted_participants, required_participants))
    participants = list(account_ids)
    picker.shuffle(participants)
    participants = participants[:participant_count]
    plan: list[int] = []
    usage: Counter[int] = Counter()
    while len(plan) < message_count:
        eligible = [account_id for account_id in participants if usage[account_id] < max_per]
        if not eligible:
            break
        account_id = eligible[len(plan) % len(eligible)]
        plan.append(account_id)
        usage[account_id] += 1
    return plan


def _system_user(session: Session, tenant_id: int):
    return require_system_user_core_features(
        session,
        tenant_id,
        service_name="持续任务服务",
        missing_message="no tenant app user available for continuous campaign runner",
    )


def _active_selected_account_ids(session: Session, campaign: Campaign, group_id: int) -> list[int]:
    accounts = load_selected_accounts_for_group(session, campaign, group_id)
    active_ids: list[int] = []
    for account in accounts:
        link = session.scalar(
            select(TgGroupAccount).where(
                TgGroupAccount.tenant_id == campaign.tenant_id,
                TgGroupAccount.group_id == group_id,
                TgGroupAccount.account_id == account.id,
                TgGroupAccount.can_send.is_(True),
            )
        )
        if link and account.status == AccountStatus.ACTIVE.value:
            active_ids.append(account.id)
    return active_ids


def _sync_campaign_ai_usage(session: Session, campaign: Campaign) -> None:
    campaign.used_ai_tokens = int(
        session.scalar(
            select(func.coalesce(func.sum(AiUsageLedger.total_tokens), 0)).where(AiUsageLedger.campaign_id == campaign.id)
        )
        or 0
    )


def _stop_if_needed(campaign: Campaign) -> bool:
    now_value = _now()
    if campaign.ends_at and campaign.ends_at <= now_value:
        campaign.status = TaskStatus.COMPLETED.value
        campaign.last_error = ""
        campaign.consecutive_failure_count = 0
        campaign.next_run_at = None
        return True
    if campaign.execution_mode == "ai_activity" and campaign.max_ai_tokens and campaign.used_ai_tokens >= campaign.max_ai_tokens:
        campaign.status = TaskStatus.COMPLETED.value
        campaign.last_error = "AI Token 上限已达到"
        campaign.consecutive_failure_count = 0
        campaign.next_run_at = None
        return True
    return False


def _pause_for_inactive_subscription(campaign: Campaign) -> None:
    campaign.status = TaskStatus.PAUSED.value
    campaign.last_run_at = _now()
    campaign.last_error = SUBSCRIPTION_INACTIVE_DETAIL
    campaign.next_run_at = None


def _due_for_run(campaign: Campaign) -> bool:
    if campaign.status not in ACTIVE_CONTINUOUS_STATUSES or campaign.execution_mode not in CONTINUOUS_MODES:
        return False
    if campaign.next_run_at is not None:
        return campaign.next_run_at <= _now()
    if campaign.last_run_at is None:
        return True
    return campaign.last_run_at + timedelta(seconds=max(1, campaign.run_interval_seconds)) <= _now()


def _mark_campaign_run_success(campaign: Campaign) -> None:
    now_value = _now()
    campaign.last_run_at = now_value
    campaign.consecutive_failure_count = 0
    campaign.next_run_at = now_value + timedelta(seconds=max(1, campaign.run_interval_seconds))
    campaign.status = TaskStatus.RUNNING.value
    campaign.last_error = ""


def _mark_campaign_run_failure(campaign: Campaign, error: str) -> None:
    now_value = _now()
    failure_count = max(0, campaign.consecutive_failure_count or 0) + 1
    base_interval = max(1, campaign.run_interval_seconds)
    backoff_seconds = min(base_interval * (2 ** (failure_count - 1)), CAMPAIGN_BACKOFF_CAP_SECONDS)
    campaign.last_run_at = now_value
    campaign.consecutive_failure_count = failure_count
    campaign.next_run_at = now_value + timedelta(seconds=backoff_seconds)
    campaign.last_error = error
    campaign.status = TaskStatus.RUNNING.value


def _record_processed_message(
    session: Session,
    *,
    campaign: Campaign,
    message: GroupContextMessage,
    target_group_id: int,
    action: str,
    reason: str,
    content: str,
) -> None:
    existing = session.scalar(
        select(CampaignProcessedMessage.id).where(
            CampaignProcessedMessage.campaign_id == campaign.id,
            CampaignProcessedMessage.source_group_id == message.group_id,
            CampaignProcessedMessage.source_remote_message_id == message.remote_message_id,
            CampaignProcessedMessage.target_group_id == target_group_id,
        )
    )
    if existing:
        return
    session.add(
        CampaignProcessedMessage(
            tenant_id=campaign.tenant_id,
            campaign_id=campaign.id,
            source_group_id=message.group_id,
            source_remote_message_id=message.remote_message_id,
            target_group_id=target_group_id,
            action=action,
            reason=reason,
            content=content[:2000],
        )
    )


def _is_processed_for_target(session: Session, campaign: Campaign, message: GroupContextMessage, target_group_id: int) -> bool:
    return bool(
        session.scalar(
            select(CampaignProcessedMessage.id).where(
                CampaignProcessedMessage.campaign_id == campaign.id,
                CampaignProcessedMessage.source_group_id == message.group_id,
                CampaignProcessedMessage.source_remote_message_id == message.remote_message_id,
                CampaignProcessedMessage.target_group_id == target_group_id,
            )
        )
    )


def _unprocessed_context_messages(session: Session, campaign: Campaign, source_group: TgGroup, target_group_ids: list[int]) -> list[GroupContextMessage]:
    recent = recent_context_messages(session, source_group, source_group.listener_context_limit)
    rows: list[GroupContextMessage] = []
    for message in reversed(recent):
        if any(not _is_processed_for_target(session, campaign, message, target_group_id) for target_group_id in target_group_ids):
            rows.append(message)
    return rows


def light_rewrite_message(content: str) -> str:
    text = re.sub(r"https?://\S+|www\.\S+", "", content or "", flags=re.IGNORECASE)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"(?i)\b(reply|回复)\s+[^:：]{0,80}[:：]", "", text)
    text = re.sub(r"(?im)^\s*(回复|reply)\s*[:：].*$", "", text)
    text = re.sub(r"([！？!?。,.，])\1+", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip(" -_，,。")
    return text[:1000]


def _rewrite_mirror_content(
    session: Session,
    *,
    campaign: Campaign,
    target_group: TgGroup,
    message: GroupContextMessage,
) -> tuple[str, str, str]:
    tenant_setting = get_tenant_ai_setting(session, campaign.tenant_id)
    provider = pick_ai_provider(session, campaign, tenant_setting)
    if not tenant_setting.ai_enabled or not provider:
        return light_rewrite_message(message.content), "template_fallback", "AI 未启用或没有健康供应商"
    prompt = (
        "请把源群消息改写成适合目标 Telegram 群发送的一条自然消息。\n"
        "要求：保留原意，换一种表达；不要暴露转发、监听、AI、运营；不要 @ 用户；不要直接引用原消息；只输出 JSON。\n"
        f"目标群：{target_group.title}\n"
        f"目标话题：{campaign.topic or target_group.topic_direction}\n"
        f"源消息发送者：{message.sender_name}\n"
        f"源消息：{message.content}\n"
        '输出格式：{"drafts":[{"persona":"自然群友","content":"改写后的消息","risk_level":"低"}]}'
    )
    try:
        result = ai_gateway.generate_drafts(
            ai_provider_credentials(provider),
            prompt,
            count=1,
            topic=campaign.topic or target_group.topic_direction,
            tone="自然、口语化、和原文不完全一样",
            persona_set=["自然群友"],
            temperature=tenant_setting.temperature,
            max_tokens=max(tenant_setting.max_tokens, 512),
        )
        candidate = result.candidates[0]
        content = candidate.content.strip()
        if not content:
            raise RuntimeError("AI 润色返回空内容")
        return content, provider.provider_type, ""
    except Exception as exc:  # noqa: BLE001 - mirror forwarding has a local fallback.
        return light_rewrite_message(message.content), "template_fallback", str(exc)


def _auto_queue_draft(session: Session, draft: AiDraft, *, actor: str, task_index: int, target_group_id: int, preferred_account_id: int | None = None) -> int:
    if preferred_account_id:
        draft.suggested_account_id = preferred_account_id
    draft.status = TaskStatus.APPROVED.value
    build_message_task_from_draft(session, draft, actor, task_index, target_group_id)
    return 1


def run_ai_activity_campaign(session: Session, campaign: Campaign) -> int:
    _sync_campaign_ai_usage(session, campaign)
    if _stop_if_needed(campaign):
        session.commit()
        return 0
    try:
        user = _system_user(session, campaign.tenant_id)
    except ValueError as exc:
        if str(exc) != SUBSCRIPTION_INACTIVE_DETAIL:
            raise
        _pause_for_inactive_subscription(campaign)
        session.commit()
        return 0

    queued = 0
    task_index = 0
    tenant_setting = get_tenant_ai_setting(session, campaign.tenant_id)
    for group_id in campaign_target_group_ids(campaign):
        group = session.get(TgGroup, group_id)
        if not group or group.tenant_id != campaign.tenant_id or group.auth_status != GroupAuthStatus.AUTHORIZED.value:
            continue
        selected_ids = _active_selected_account_ids(session, campaign, group.id)
        if not selected_ids:
            continue
        message_count = min(len(selected_ids), max(1, campaign.max_drafts_per_batch), 50)
        ratio = random.uniform(campaign.participation_min_ratio, campaign.participation_max_ratio)
        account_plan = build_participation_plan(
            selected_ids,
            message_count,
            ratio=ratio,
            max_messages_per_account=campaign.max_messages_per_account,
        )
        if not account_plan:
            continue
        contexts = recent_context_messages(session, group, group.listener_context_limit)
        payload = GenerateDraftsRequest(
            count=len(account_plan),
            tone="自然、像真实群成员聊天，避免刷屏，接住上下文",
            use_ai=True,
            fallback_to_mock=tenant_setting.fallback_to_mock,
            selected_account_ids_by_group={str(group.id): selected_ids},
            target_group_id=group.id,
            conversation_context=[
                {
                    "sender_name": item.sender_name,
                    "content": item.content,
                    "sent_at": item.sent_at.isoformat() if item.sent_at else None,
                }
                for item in reversed(contexts)
            ],
        )
        drafts = generate_drafts(session, campaign.id, payload, user, auto_commit=False)
        ensure_task_quota_available(session, campaign.tenant_id, len(drafts))
        for index, draft in enumerate(drafts):
            target_group = session.get(TgGroup, draft.group_id)
            if not target_group:
                continue
            filtered = filter_outbound_content(
                session,
                tenant_id=campaign.tenant_id,
                group=target_group,
                content=draft.content,
                reject_mentions=True,
                reject_replies=True,
            )
            if not filtered.ok:
                draft.status = TaskStatus.REJECTED.value
                draft.generation_error = filtered.reason
                campaign.filtered_count += 1
                continue
            draft.content = filtered.content
            preferred_account_id = account_plan[index] if index < len(account_plan) else None
            queued += _auto_queue_draft(
                session,
                draft,
                actor="持续AI活跃任务",
                task_index=task_index,
                target_group_id=target_group.id,
                preferred_account_id=preferred_account_id,
            )
            task_index += 1
    _sync_campaign_ai_usage(session, campaign)
    _mark_campaign_run_success(campaign)
    if _stop_if_needed(campaign):
        campaign.last_run_at = _now()
    audit(session, tenant_id=campaign.tenant_id, actor="持续任务服务", action="执行AI活跃任务", target_type="campaign", target_id=str(campaign.id), detail=f"queued={queued}; tokens={campaign.used_ai_tokens}")
    session.commit()
    return queued


def run_mirror_forward_campaign(session: Session, campaign: Campaign) -> int:
    if _stop_if_needed(campaign):
        session.commit()
        return 0
    try:
        _system_user(session, campaign.tenant_id)
    except ValueError as exc:
        if str(exc) != SUBSCRIPTION_INACTIVE_DETAIL:
            raise
        _pause_for_inactive_subscription(campaign)
        session.commit()
        return 0

    queued = 0
    task_index = 0
    target_group_ids = campaign_target_group_ids(campaign)
    source_group_ids = parse_id_list(campaign.source_group_ids)
    for source_group_id in source_group_ids:
        source_group = session.get(TgGroup, source_group_id)
        if not source_group or source_group.tenant_id != campaign.tenant_id:
            continue
        collect_group_context(session, source_group)
        for message in _unprocessed_context_messages(session, campaign, source_group, target_group_ids):
            for target_group_id in target_group_ids:
                if _is_processed_for_target(session, campaign, message, target_group_id):
                    continue
                target_action = "filtered"
                target_reason = ""
                target_group = session.get(TgGroup, target_group_id)
                if not target_group or target_group.tenant_id != campaign.tenant_id:
                    continue
                selected_ids = _active_selected_account_ids(session, campaign, target_group.id)
                if not selected_ids:
                    target_reason = "目标群没有可用发送账号"
                    continue
                outbound_content, generation_source, rewrite_error = _rewrite_mirror_content(
                    session,
                    campaign=campaign,
                    target_group=target_group,
                    message=message,
                )
                filtered = filter_outbound_content(
                    session,
                    tenant_id=campaign.tenant_id,
                    group=target_group,
                    content=outbound_content,
                    reject_mentions=True,
                    reject_replies=True,
                )
                if not filtered.ok:
                    target_reason = filtered.reason
                    campaign.filtered_count += 1
                    _record_processed_message(
                        session,
                        campaign=campaign,
                        message=message,
                        target_group_id=target_group.id,
                        action=target_action,
                        reason=target_reason,
                        content=outbound_content,
                    )
                    continue
                ensure_task_quota_available(session, campaign.tenant_id)
                draft = AiDraft(
                    tenant_id=campaign.tenant_id,
                    campaign_id=campaign.id,
                    group_id=target_group.id,
                    persona="源群同步",
                    content=filtered.content,
                    risk_level="低",
                    provider_name="监听转发" if generation_source != "template_fallback" else "代码轻改写",
                    model_name=generation_source,
                    prompt_template_name="AI 润色监听转发" if generation_source != "template_fallback" else "代码轻改写降级",
                    suggested_account_id=selected_ids[task_index % len(selected_ids)],
                    sequence_index=task_index + 1,
                    generation_source=generation_source,
                    generation_error=rewrite_error,
                    status=TaskStatus.APPROVED.value,
                )
                session.add(draft)
                session.flush()
                build_message_task_from_draft(session, draft, "监听转发任务", task_index, target_group.id)
                queued += 1
                task_index += 1
                _record_processed_message(
                    session,
                    campaign=campaign,
                    message=message,
                    target_group_id=target_group.id,
                    action="queued",
                    reason=f"ai_failed_template_fallback:{rewrite_error[:500]}" if generation_source == "template_fallback" and rewrite_error else "queued=1",
                    content=filtered.content,
                )
    _mark_campaign_run_success(campaign)
    if _stop_if_needed(campaign):
        campaign.last_run_at = _now()
    audit(session, tenant_id=campaign.tenant_id, actor="持续任务服务", action="执行监听转发任务", target_type="campaign", target_id=str(campaign.id), detail=f"queued={queued}; sources={source_group_ids}")
    session.commit()
    return queued


def process_continuous_campaign(session: Session, campaign_id: int) -> int:
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.execution_mode not in CONTINUOUS_MODES:
        return 0
    try:
        if campaign.execution_mode == "ai_activity":
            return run_ai_activity_campaign(session, campaign)
        if campaign.execution_mode == "mirror_forward":
            return run_mirror_forward_campaign(session, campaign)
        return 0
    except Exception as exc:  # noqa: BLE001 - operator-facing task status.
        session.rollback()
        campaign = session.get(Campaign, campaign_id)
        if campaign:
            _mark_campaign_run_failure(campaign, str(exc))
            session.commit()
        return 0


def drain_continuous_campaigns(session_factory, limit: int = 5) -> int:
    with session_factory() as session:
        rows = list(
            session.scalars(
                select(Campaign)
                .where(Campaign.execution_mode.in_(CONTINUOUS_MODES), Campaign.status.in_(ACTIVE_CONTINUOUS_STATUSES))
                .order_by(Campaign.next_run_at.asc().nullsfirst(), Campaign.last_run_at.asc().nullsfirst(), Campaign.id.asc())
                .limit(limit)
            )
        )
        campaign_ids = [campaign.id for campaign in rows if _due_for_run(campaign) or _stop_if_needed(campaign)]
        session.commit()
    processed = 0
    for campaign_id in campaign_ids[:limit]:
        with session_factory() as session:
            processed += process_continuous_campaign(session, campaign_id)
    return processed


def cancel_campaign(session: Session, campaign_id: int, actor: str) -> Campaign:
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        raise ValueError("campaign not found")
    if campaign.status in {TaskStatus.SENT.value, TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value}:
        return campaign
    campaign.status = TaskStatus.CANCELLED.value
    campaign.last_error = ""
    campaign.next_run_at = None
    audit(session, tenant_id=campaign.tenant_id, actor=actor, action="取消运营任务", target_type="campaign", target_id=str(campaign.id))
    session.commit()
    session.refresh(campaign)
    return campaign


__all__ = [
    "build_participation_plan",
    "cancel_campaign",
    "drain_continuous_campaigns",
    "process_continuous_campaign",
    "run_ai_activity_campaign",
    "run_mirror_forward_campaign",
]
