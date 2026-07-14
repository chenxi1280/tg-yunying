from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.models import (
    AiAccountGroupStanceMemory,
    AiAccountVoiceProfile,
    AiGroupMessageMemory,
    Task,
    Tenant,
    TenantAiSetting,
    TgAccount,
    TgAccountOnlineState,
    TgGroup,
    TgGroupAccount,
)
from app.services.task_center.ai_message_memory import mark_group_ai_message_result, reserve_group_ai_message


@dataclass(frozen=True)
class AiPlannerScenario:
    task_id: str
    task_name: str
    profile_summaries: tuple[str, ...]
    messages_per_round: int = 1
    force_bootstrap: bool = False
    tenant_ai_enabled: bool = False
    emoji_policy: str = ""
    profile_versions: tuple[int, ...] = ()
    stance_summary: str = ""
    include_previous_photo_memory: bool = False
    max_concurrent: int = 10
    include_pacing: bool = True
    include_reply_min: bool = True
    include_allow_repeat: bool = True
    include_silent_mode: bool = True
    include_low_confidence: bool = True


def seed_ai_planner_scope(session, now_value, scenario: AiPlannerScenario) -> Task:
    session.add(Tenant(id=1, name="默认运营空间"))
    if scenario.tenant_ai_enabled:
        session.add(TenantAiSetting(tenant_id=1, ai_enabled=True))
    session.add(TgGroup(
        id=7,
        tenant_id=1,
        tg_peer_id="-1007",
        title="运营群",
        auth_status="已授权运营",
        can_send=True,
    ))
    _add_accounts(session, now_value, scenario)
    _add_optional_memories(session, now_value, scenario)
    task = _scenario_task(scenario)
    session.add(task)
    session.commit()
    return task


def _add_accounts(session, now_value, scenario: AiPlannerScenario) -> None:
    for index, summary in enumerate(scenario.profile_summaries, 11):
        session.add(TgAccount(
            id=index,
            tenant_id=1,
            display_name=f"账号{chr(54 + index)}",
            phone_masked=f"+861***00{index}",
            status="在线",
            session_ciphertext=f"session-{chr(86 + index)}",
        ))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=index, can_send=True))
        session.add(TgAccountOnlineState(
            tenant_id=1,
            account_id=index,
            desired_online=True,
            online_status="online",
            stale_after_at=now_value + timedelta(minutes=5),
        ))
        version = scenario.profile_versions[index - 11] if scenario.profile_versions else 1
        session.add(_profile(index, summary, scenario.emoji_policy if index == 11 else "", version=version))


def _profile(account_id: int, summary: str, emoji_policy: str, *, version: int) -> AiAccountVoiceProfile:
    return AiAccountVoiceProfile(
        tenant_id=1,
        account_id=account_id,
        version=version,
        status="active",
        quality_status="active",
        short_prompt_summary=summary,
        emoji_policy=emoji_policy or None,
    )


def _add_optional_memories(session, now_value, scenario: AiPlannerScenario) -> None:
    if scenario.stance_summary:
        session.add(AiAccountGroupStanceMemory(
            tenant_id=1,
            group_id=7,
            account_id=11,
            summary=scenario.stance_summary,
        ))
    if scenario.include_previous_photo_memory:
        session.add(AiGroupMessageMemory(
            id="previous-photo-memory",
            tenant_id=1,
            group_id=7,
            task_id="another-task",
            account_id=11,
            raw_text="昨天照片准",
            normalized_text="昨天照片准",
            text_fingerprint="previous-photo-memory",
            semantic_cluster="photo_accuracy",
            status="success",
            planned_at=now_value - timedelta(minutes=1),
            sent_at=now_value - timedelta(minutes=1),
        ))


def _scenario_task(scenario: AiPlannerScenario) -> Task:
    return Task(
        id=scenario.task_id,
        tenant_id=1,
        name=scenario.task_name,
        type="group_ai_chat",
        status="running",
        account_config={
            "selection_mode": "all",
            "max_concurrent": scenario.max_concurrent,
            "cooldown_per_account_minutes": 0,
        },
        pacing_config={"max_actions_per_hour": 120} if scenario.include_pacing else {},
        type_config=_scenario_type_config(scenario),
        stats={"force_bootstrap_once": True} if scenario.force_bootstrap else {},
    )


def _scenario_type_config(scenario: AiPlannerScenario) -> dict:
    config = {
        "target_group_id": 7,
        "messages_per_round_mode": "manual",
        "messages_per_round": scenario.messages_per_round,
        "fact_anchor_required": False,
    }
    if scenario.include_reply_min:
        config["reply_min_per_round"] = 0
    if scenario.include_allow_repeat:
        config["allow_account_repeat"] = False
    if scenario.include_silent_mode:
        config["silent_mode_enabled"] = False
    if scenario.include_low_confidence:
        config["low_confidence_silence_enabled"] = False
    return config


def seed_sent_memory(session, now_value, *, text: str) -> None:
    memory = reserve_group_ai_message(
        session,
        tenant_id=1,
        group_id=7,
        task_id="previous-task",
        account_id=11,
        raw_text=text,
        now=now_value - timedelta(minutes=1),
    )
    mark_group_ai_message_result(session, memory.id, status="success", action_id="previous-action")
    session.commit()
