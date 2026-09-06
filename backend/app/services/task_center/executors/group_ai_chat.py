from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from difflib import SequenceMatcher
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, case, func, or_, select, union, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    Action,
    AiCoverageVariationIntent,
    AiGroupMessageMemory,
    ContentMixCycle,
    ContentMixCycleSlot,
    ExecutionAttempt,
    GroupBotAdmission,
    OperationTarget,
    RuleSet,
    Task,
    TaskAccountDailyCoverage,
    TaskDayLedger,
    TaskGroupBotAdmission,
    TaskGroupDailyMessageSlot,
    TaskGroupDailyTarget,
    TgGroup,
)
from app.services._common import _now
from app.services.account_online_readiness import online_ready_account_ids_for_planning
from app.services.account_capacity import (
    AccountCapacityCache,
    AccountCapacityReservation,
    available_accounts_by_capacity,
    next_capacity_window,
)
from app.services.content_filters import contains_coarse_language, looks_like_generated_template_noise, looks_like_operator_ui_content
from app.services.group_listeners import recent_context_messages
from app.services.target_learning_audit import audit_learning_profile_use
from app.services.tenant_target_profile import tenant_learning_profile_preview
from app.services.rule_engine import bound_rule_version, evaluate_input_filter

from ..account_pacing_guard import (
    AccountPacingDeadlineExceeded,
    AccountPacingLockUnavailable,
    bind_account_pacing_reservation,
    reserve_account_pacing,
)
from ..account_pool import DAILY_COVERAGE_SUCCESS_STATUSES, daily_uncovered_account_count, select_task_accounts
from ..ai_pacing import AiPacingAssignment, assign_ai_pacing_slots
from ..account_scope import bootstrap_missing_all_account_task_scope
from ..ai_act_types import canonical_ai_group_act_type
from ..ai_generator import AI_GENERATION_UNAVAILABLE_MESSAGE
from ..ai_group_content_allocation import freeze_content_intents
from ..ai_group_content_intent_support import GenericWarmupQuestionWait
from ..ai_message_memory import mark_group_ai_message_result, reserve_group_ai_message
from ..ai_reply_allocation import reply_requirement_for_plan
from ..account_voice_profile_generation_jobs import enqueue_voice_profile_generation
from ..account_voice_profiles import group_stance_summaries, voice_profile_prompt_details
from ..channel_membership import gate_channel_membership
from ..config_normalization import (
    apply_default_rule_binding,
    apply_group_ai_account_coverage_defaults,
)
from ..content_mix_cycles import (
    ContentMixCycleSpec,
    ContentMixSlotSpec,
    create_content_mix_cycle,
    mark_cycle_slot_materialized,
)
from ..coverage_capacity import (
    HARD_HOURLY_GROUP_COOLDOWN_BLOCKED_MESSAGE,
    hard_hourly_group_cooldown_proof,
)
from ..datetime_compat import parse_zone, to_zone
from ..daily_coverage import (
    backfill_daily_coverage_confirmations,
    bind_coverage_reservation,
    block_coverage_accounts,
    block_voice_profile_coverage,
    ensure_task_daily_coverage,
    ready_coverage_rows,
    release_coverage_reservation,
    release_voice_profile_coverage_for_check_in,
    release_planned_coverage_reservation,
    reserve_coverage_for_planned_action,
    VOICE_PROFILE_MISSING_BLOCKER_CODE,
    VOICE_PROFILE_MISSING_MESSAGE,
)
from ..daily_coverage_planning import (
    MAX_DAILY_COVERAGE_PLAN_BATCH,
    advance_coverage_plan_cursor,
    coverage_plan_totals,
    has_no_terminal_shortfall_projection,
    ready_coverage_plan_batch,
)
from ..content_mix_replan_recovery import recover_stale_pending_content_mix_slots
from ..daily_ledgers import (
    bind_unowned_group_slots_to_coverage,
    ensure_task_day_ledger,
)
from ..engagement_participation import (
    ensure_daily_participation_plan,
)
from ..engagement_natural_opportunity import ensure_natural_opportunity_plan
from ..engagement_portfolio import reserve_portfolio_units
from ..engagement_group_scope import group_operation_target, sync_group_participation_scope
from ..engagement_planning_admission import ensure_planning_admission_snapshot
from ..daily_group_target import (
    daily_group_due_message_count,
    ensure_task_group_daily_target,
)
from ..direct_check_in import MASK_MISSING_CHECK_IN_SOURCE
from ..daily_fulfillment import record_daily_fulfillment_decision
from ..fingerprints import fingerprint_exists, remember_fingerprint
from ..fulfillment_activation import CURRENT_CONTRACT_VERSION
from ..group_ai_scope import (
    remotely_invalid_reply_target_ids,
    successful_own_history_reply_facts,
)
from ..group_bot_admission import plannable_admission_account_ids
from .group_ai_extra_candidates import (
    DailyGroupExtraCandidateSpec,
    daily_group_extra_candidate_ids,
)
from ..hard_hourly import (
    current_progress,
    enabled as hard_hourly_enabled,
    hard_schedule_times,
    mark_plan_result,
    planning_rate as hard_hourly_planning_rate,
)
from ..legacy_anchor_rewrite import (
    expire_incomplete_daily_contract_actions,
    expire_legacy_anchor_rewritten_actions,
)
from ..pacing import (
    current_hour_rounds,
    operation_intensity,
    schedule_due_times,
    schedule_times,
    task_pacing_anchor,
)
from ..pacing_persistence import freeze_action_pacing, freeze_pacing_owner
from ..payloads import SendMessagePayload, create_send_action
from ..schedule_reservation import reserve_task_schedule_times
from ..source_pacing import (
    SourcePacingSlot,
    latest_wall_datetime,
    schedule_source_pacing_points,
    source_pacing_plan_hash,
    wall_datetime,
)
from ..source_capacity_plans import apply_source_capacity_plan
from ..targets import group_from_reference
from .common import stats_inc


WAITING_NEW_CONTEXT_MESSAGE = "暂无新的真人上下文，等待群内新消息"
WAITING_IDLE_CONTINUATION_MESSAGE = "持续监听中，等待新消息或空闲续聊间隔"
AI_QUALITY_ANCHOR_SKIP_MESSAGE = "AI 候选缺少事实锚点，已跳过本轮"
AI_QUALITY_DUPLICATE_SKIP_MESSAGE = "AI 候选语义重复风险过高，已跳过本轮"
ACCOUNT_CAPACITY_BLOCKED_MESSAGE = "账号容量已排满，等待账号额度恢复后继续执行"
ACCOUNT_COOLDOWN_BLOCKED_MESSAGE = "账号冷却中，等待冷却后继续执行"
ACCOUNT_UNAVAILABLE_MESSAGE = "没有可用账号，等待账号恢复后继续执行"
ACCOUNT_DISTRIBUTION_SKEW_MESSAGE = "账号分布偏斜，已阻断本轮硬目标规划"
ALL_ACCOUNT_DAILY_COVERAGE_REPLAN_CODE = "all_account_daily_coverage_replan"
ALL_ACCOUNT_DAILY_COVERAGE_REPLAN_MESSAGE = "任务已切换为全部账号每日覆盖，旧硬目标规划已跳过等待按覆盖账本重建"
DEFAULT_IDLE_CONTINUATION_SECONDS = 300
DEFAULT_CONTEXT_BOUND_SCHEDULE_WINDOW_SECONDS = 300
MIN_CONTEXT_BOUND_SCHEDULE_WINDOW_SECONDS = 60
DAILY_COVERAGE_DEBT_RECHECK_SECONDS = 120
GROUP_CHAT_SCENE = "group_chat"
ALL_ACCOUNT_COVERAGE_CAPACITY_BLOCKED_MESSAGE = "全部账号每日覆盖容量不足，已停止创建发送 Action"
SENDABLE_COVERAGE_CAPACITY_BLOCKED_MESSAGE = "当前可发账号每日覆盖容量不足，已停止创建发送 Action"
GROUP_BOT_PLANNABLE_STATES = frozenset({
    "group_bot_admission_ready",
    "post_follow_visibility_probe",
})
CONTENT_MIX_REPLAN_PRESERVED_FIELDS = (
    "cycle_id",
    "slot_id",
    "content_mix_contract_version",
    "rule_set_id",
    "rule_set_version_id",
    "resolved_rule_set_version_id",
    "rule_set_version",
    "rule_trace",
    "material_intent",
    "allow_material",
)
FACT_FIRST_REBUILD_ACTION_STATUSES = frozenset({
    "failed",
    "retryable_failed",
    "skipped",
})
FACT_FIRST_STALE_RECOVERY_BATCH_LIMIT = MAX_DAILY_COVERAGE_PLAN_BATCH


@dataclass(frozen=True)
class CoveragePlanState:
    rows: list[TaskAccountDailyCoverage]
    rows_by_account: dict[int, TaskAccountDailyCoverage]
    due_debt: int
    account_count: int = 0
    target_per_account: int = 1
    confirmed_count: int = 0
    reserved_count: int = 0
    sendable_account_count: int = 0
    sendable_confirmed_count: int = 0
    sendable_reserved_count: int = 0
    required_new: int = 0
    daily_group_target_id: str = ""
    effective_daily_target: int = 0
    due_message_count: int = 0
    confirmed_message_count: int = 0
    volume_need_now: int = 0
    deadline_at: datetime | None = None


CHAT_MODE_REPLY = "reply"
CHAT_MODE_IDLE_WARMUP = "idle_warmup"
CHAT_MODE_BOOTSTRAP = "bootstrap"
AI_CHAT_ROUND_INTERVALS_SECONDS = {
    "高峰期": (20, 60),
    "正常期": (45, 120),
    "启动期": (60, 180),
    "低频期": (180, 360),
    "休眠期": (600, 1200),
    "静默期": (300, 900),
}
HARD_HOURLY_MIN_BATCH_MESSAGES = 10
HARD_HOURLY_MIN_DISTRIBUTION_ACTIONS = 3
HARD_HOURLY_MAX_CONSECUTIVE_ACCOUNT_RUN = 1
HARD_HOURLY_MIN_DISTRIBUTED_ACCOUNTS = 2
ACCOUNT_OFFLINE_SAMPLE_LIMIT = 10
DEFERRED_AI_HISTORY_MAX_CHARS = 1000
QUALITY_REJECTION_SAMPLE_LIMIT = 5
VOICE_PROFILE_MATCH_SCORE = 100
VOICE_PROFILE_MISMATCH_SCORE = 0
VOICE_PROFILE_LONG_SHORT_SENTENCE_LIMIT = 80
EMOJI_PATTERN = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
RECENT_CYCLE_SCAN_LIMIT = 200
RECENT_PLANNED_AI_STATUSES = ("pending", "claiming", "executing", "unknown_after_send")
RECENT_TARGET_USAGE_STATUSES = (*RECENT_PLANNED_AI_STATUSES, "success")
IN_FLIGHT_REPLY_TARGET_USAGE_STATUSES = RECENT_PLANNED_AI_STATUSES
RECENT_TARGET_USAGE_MEMORY_STATUSES = ("reserved", "success", "unknown_after_send")
RECENT_TARGET_USAGE_SCAN_LIMIT = 120
VOICE_PROFILE_REPLAN_OPEN_STATUSES = ("pending", "retryable_failed")
ACTIVE_PROFILE_MATCH_SCORE = 100
UNAVAILABLE_PROFILE_MATCH_SCORE = 0
CAUTIOUS_STANCE_MARKERS = ("观望", "质疑", "别突然强夸", "保留", "再看看", "谨慎")
STRONG_POSITIVE_MARKERS = ("绝对可以", "闭眼冲", "非常靠谱", "很靠谱", "稳了", "必须冲", "放心冲", "强推", "真好")
VAGUE_AI_FILLER_MARKERS = ("确实不错", "感觉挺靠谱", "挺靠谱", "可以关注一下", "有点意思", "看起来还行")
VAGUE_AI_FILLER_DETAIL_MARKERS = (
    "价格",
    "多少",
    "怎么",
    "哪",
    "问",
    "照片",
    "位置",
    "反馈",
    "身材",
    "服务",
    "新妹子",
    "上榜",
    "药",
)
@dataclass(frozen=True)
class PlanAbort:
    created: int = 0


@dataclass(frozen=True)
class PlanFacts:
    config: dict
    task_id: str
    task_config_revision: int
    hard_progress: dict
    rule_version: Any
    rule_set: RuleSet | None
    target: OperationTarget | None
    group: TgGroup
    coverage: CoveragePlanState
    target_label: str
    continuity_reply_targets: tuple[dict, ...] = ()


@dataclass(frozen=True)
class AccountPlanState:
    accounts: list[Any]


@dataclass(frozen=True)
class ContextPlanState:
    history_depth: int
    fingerprint_source: str
    usable_rows: list[Any]
    unprocessed_rows: list[Any]
    previous_ai_messages: list[Any]
    mode: str
    ramp_ratio: float
    idle_continuation: bool


@dataclass(frozen=True)
class TurnPlanState:
    cycle_index: int
    round_config: dict
    selected: list[Any]
    turn_count: int
    history: str


@dataclass(frozen=True)
class ProfilePlanState:
    selected: list[Any]
    topic_thread: str
    topic_plan: str
    account_memories: dict
    account_prompt_profiles: dict
    voice_profiles: dict
    stance_summaries: dict
    profile_preview: dict
    coverage_counts: dict
    coverage_rows: dict
    cycle_id: str


@dataclass(frozen=True)
class GenerationPlanState:
    quality_items: list[dict]
    times: list[datetime]
    requested_reply_count: int
    coverage_reply_shortfall: bool
    burst_plan: dict
    chat_mode: str
    context_message_ids: list[int]
    generation_source: str


@dataclass(frozen=True)
class PlanBlueprint:
    facts: PlanFacts
    context: ContextPlanState
    turn: TurnPlanState
    profile: ProfilePlanState
    generation: GenerationPlanState


@dataclass(frozen=True)
class SlotBuildInput:
    blueprint: PlanBlueprint
    account: Any
    index: int
    item: dict
    planned_at: datetime


@dataclass(frozen=True)
class SlotSnapshot:
    account_id: int
    planned_at: datetime
    payload: SendMessagePayload
    pacing_owner: TaskGroupDailyMessageSlot | None = None
    pacing_slot_key: str = ""
    pacing_reservation: Any | None = None


@dataclass(frozen=True)
class PreparedActionPlan:
    slots: list[SlotSnapshot]
    hard_blockers: dict[str, int]


@dataclass(frozen=True)
class FrozenContentMix:
    cycle: ContentMixCycle
    slots_by_logical_id: dict[str, ContentMixCycleSlot]


@dataclass(frozen=True)
class ContentMixReplanResult:
    found: bool
    created: int = 0


@dataclass(frozen=True)
class QuantitySlotAlignmentResult:
    code: str
    ledger_id: str
    slots: tuple[TaskGroupDailyMessageSlot, ...]
    requested_count: int
    missing_coverage_ids: tuple[str, ...] = ()
    missing_extra_count: int = 0

    @property
    def aligned_count(self) -> int:
        return len(self.slots)


class QuantitySlotAlignmentError(RuntimeError):
    def __init__(self, result: QuantitySlotAlignmentResult) -> None:
        self.result = result
        super().__init__(result.code)


def _load_plan_facts(session: Session, task: Task) -> PlanFacts | PlanAbort:
    config = {**(task.type_config or {}), "pacing_config": task.pacing_config or {}}
    config = _canonicalized_task_config(session, task, config)
    config = _bind_legacy_default_rules(session, task, config)
    progress: dict = {}
    rule_version = _required_group_rule_version(session, task, progress)
    if rule_version is None:
        return PlanAbort()
    rule_set = session.get(RuleSet, rule_version.rule_set_id)
    target_id = int(config.get("target_operation_target_id") or 0)
    target = session.get(OperationTarget, target_id) if target_id else None
    group = None
    if int(config.get("target_group_id") or 0):
        group = _resolve_plan_group(session, task, config, progress=progress)
        if isinstance(group, PlanAbort):
            return group
    gate_abort = _target_membership_abort(session, task, target, progress=progress)
    if gate_abort:
        return gate_abort
    if group is None:
        group = _resolve_plan_group(session, task, config, progress=progress)
        if isinstance(group, PlanAbort):
            return group
    gate_abort = _plan_outbound_target_abort(session, task, target, group, progress)
    if gate_abort:
        return gate_abort
    coverage = _coverage_plan_state(
        session,
        task,
        group,
        config=config,
        progress=progress,
    )
    _record_daily_coverage_next_check(task, coverage.required_new > 0)
    active_config = _with_active_conversation_targets(session, task, config, group)
    return _build_plan_facts(
        session, task, group=group, target=target, config=active_config,
        progress=progress, rule_version=rule_version, rule_set=rule_set,
        coverage=coverage,
    )


def _build_plan_facts(
    session: Session,
    task: Task,
    *,
    group: TgGroup,
    target: OperationTarget | None,
    config: dict,
    progress: dict,
    rule_version: Any,
    rule_set: RuleSet | None,
    coverage: CoveragePlanState,
) -> PlanFacts:
    label = target.title if target and target.tenant_id == task.tenant_id else group.title
    continuity_targets = _interaction_continuity_targets(
        session, task, group, coverage, config, target,
    )
    return PlanFacts(
        config=config,
        task_id=task.id,
        task_config_revision=int(task.config_revision or 1),
        hard_progress=progress,
        rule_version=rule_version,
        rule_set=rule_set,
        target=target,
        group=group,
        coverage=coverage,
        target_label=label,
        continuity_reply_targets=continuity_targets,
    )


def _interaction_continuity_targets(
    session: Session,
    task: Task,
    group: TgGroup,
    coverage: CoveragePlanState,
    config: dict,
    target: OperationTarget | None,
) -> tuple[dict, ...]:
    if not _uses_unified_engagement(task):
        return ()
    daily_target = session.get(
        TaskGroupDailyTarget, str(coverage.daily_group_target_id or ""),
    )
    ledger = (
        session.get(TaskDayLedger, daily_target.task_day_ledger_id)
        if daily_target and daily_target.task_day_ledger_id else None
    )
    operation_target_id = int(
        target.id if target else config.get("target_operation_target_id") or 0
    )
    quantity_complete = bool(
        daily_target
        and int(daily_target.confirmed_message_count or 0)
        >= int(daily_target.effective_message_target or 0)
    )
    if ledger is None or operation_target_id <= 0 or not quantity_complete:
        return ()
    depth = int(config.get("chat_history_depth") or 50)
    rows = recent_context_messages(session, group, depth)
    from ..engagement_conversation import interaction_reply_targets
    from ..engagement_interaction_continuity import (
        ensure_interaction_continuity_capacity,
    )

    targets = interaction_reply_targets(session, task, group, context_rows=rows)
    if not targets:
        return ()
    decision = ensure_interaction_continuity_capacity(
        session, task, ledger, group,
        operation_target_id=operation_target_id,
        reply_targets=targets,
    )
    return decision.admitted_targets


def _required_group_rule_version(
    session: Session,
    task: Task,
    progress: dict,
):
    rule_version = bound_rule_version(session, task)
    if rule_version is not None:
        return rule_version
    task.last_error = task.last_error or "AI 活群任务缺少已发布规则绑定"
    stats = dict(task.stats or {})
    stats["rule_binding_missing_count"] = int(
        stats.get("rule_binding_missing_count") or 0
    ) + 1
    stats["last_plan_blocker"] = "rule_binding_missing"
    task.stats = stats
    if progress:
        _mark_hard_blocked(task, progress, "rule_binding_missing")
    return None


def _bind_legacy_default_rules(
    session: Session,
    task: Task,
    config: dict,
) -> dict:
    if str(config.get("engagement_contract_version") or "legacy_v0") != "legacy_v0":
        return config
    if config.get("rule_set_id") or config.get("rule_set_version_id"):
        return config
    bound = apply_default_rule_binding(
        session,
        task.tenant_id,
        task_type=task.type,
        config=config,
    )
    task.type_config = {
        key: value for key, value in bound.items() if key != "pacing_config"
    }
    return bound


def _target_membership_abort(
    session: Session, task: Task, target: OperationTarget | None, *, progress: dict,
) -> PlanAbort | None:
    if not target or target.tenant_id != task.tenant_id or target.target_type != "group":
        return None
    bootstrap_missing_all_account_task_scope(session, task, now=_now())
    lifecycle_abort = _plan_outbound_target_abort(session, task, target, None, progress)
    if lifecycle_abort:
        return lifecycle_abort
    gate = gate_channel_membership(session, task, target, require_send=True)
    if gate.ready:
        return None
    if progress:
        blocker = gate.blocker_reason or "target_membership_pending"
        deficit = max(1, int(progress.get("deficit") or gate.created or 1))
        mark_plan_result(task, progress, 0, {blocker: deficit})
    return PlanAbort(gate.created)


def _plan_outbound_target_abort(
    session: Session,
    task: Task,
    target: OperationTarget | None,
    group: TgGroup | None,
    progress: dict,
) -> PlanAbort | None:
    from app.services.outbound_target_gate import evaluate_outbound_target_gate

    peer_id = target.tg_peer_id if target else (group.tg_peer_id if group else "")
    block = evaluate_outbound_target_gate(
        session,
        target=target,
        group=group,
        tenant_id=task.tenant_id,
        outbound_peer=peer_id,
        require_identity=target is not None,
        include_group_policy=False,
    )
    if block is None:
        return None
    task.last_error = block.detail
    if progress:
        deficit = max(1, int(progress.get("deficit") or 1))
        mark_plan_result(task, progress, 0, {block.code: deficit})
    return PlanAbort()


def _resolve_plan_group(
    session: Session, task: Task, config: dict, *, progress: dict,
) -> TgGroup | PlanAbort:
    group = group_from_reference(
        session,
        task.tenant_id,
        group_id=int(config.get("target_group_id") or 0) or None,
        operation_target_id=int(config.get("target_operation_target_id") or 0) or None,
        require_authorized=False,
    )
    if group:
        return group
    mismatch = _configured_target_group_mismatch(session, task, config)
    blocker = "target_identity_mismatch" if mismatch else "target_permission"
    task.last_error = (
        "任务目标群与运营目标身份不一致"
        if mismatch
        else "目标群不存在或未授权"
    )
    if progress:
        deficit = max(1, int(progress.get("deficit") or 1))
        mark_plan_result(task, progress, 0, {blocker: deficit})
    return PlanAbort()


def _configured_target_group_mismatch(
    session: Session,
    task: Task,
    config: dict,
) -> bool:
    target_id = int(config.get("target_operation_target_id") or 0)
    group_id = int(config.get("target_group_id") or 0)
    if not target_id or not group_id:
        return False
    target = session.get(OperationTarget, target_id)
    group = session.get(TgGroup, group_id)
    return bool(
        target
        and group
        and target.tenant_id == task.tenant_id
        and group.tenant_id == task.tenant_id
    )


def _load_plan_accounts(
    session: Session,
    task: Task,
    facts: PlanFacts,
    *,
    include_replan_accounts: bool = True,
) -> AccountPlanState | PlanAbort:
    account_limit = _plan_account_limit(
        task,
        facts.hard_progress,
        planning_limit=session.info.get("daily_coverage_plan_limit"),
    )
    if facts.continuity_reply_targets:
        return _load_continuity_plan_accounts(
            session, task, facts, account_limit,
        )
    if _all_accounts_daily_coverage(facts.config):
        return _load_daily_coverage_plan_accounts(
            session, task, facts, account_limit,
            include_replan_accounts=include_replan_accounts,
        )
    return _load_regular_plan_accounts(session, task, facts, account_limit)


def _load_continuity_plan_accounts(
    session: Session,
    task: Task,
    facts: PlanFacts,
    account_limit: int,
) -> AccountPlanState | PlanAbort:
    continuity_config = {
        **facts.config,
        "account_coverage_mode": "selected_accounts",
        "_daily_coverage_enforced": False,
    }
    candidates = _select_accounts_for_plan(
        session, task, facts.group, {}, continuity_config, coverage_rows=[],
    )
    candidates = _online_ready_accounts(session, task, candidates, {})
    ready = _group_bot_ready_accounts_for_plan(
        session, task, facts.group, candidates,
    )
    ready = _daily_voice_profile_ready_accounts(session, task, facts, ready)
    admitted = _portfolio_continuity_accounts(
        session, task, facts, ready[:account_limit],
    )
    if admitted:
        return AccountPlanState(admitted)
    task.last_error = "interaction_continuity_account_capacity_exhausted"
    return PlanAbort()


def _portfolio_continuity_accounts(
    session: Session,
    task: Task,
    facts: PlanFacts,
    candidates: list,
) -> list:
    target = session.get(
        TaskGroupDailyTarget, str(facts.coverage.daily_group_target_id or ""),
    )
    ledger = (
        session.get(TaskDayLedger, target.task_day_ledger_id)
        if target and target.task_day_ledger_id else None
    )
    if ledger is None or not candidates:
        return []
    by_id = {int(account.id): account for account in candidates}
    admitted_ids: list[int] = []
    for reply_target in facts.continuity_reply_targets:
        claim_id = str(reply_target.get("conversation_turn_claim_id") or "")
        decision = reserve_portfolio_units(
            session, task, ledger,
            action_class="authored_message",
            demand_identity=f"interaction_continuity:{claim_id}",
            total_units=1,
            candidate_account_ids=list(by_id),
        )
        admitted_ids.extend(decision.allocated_units_by_account)
    return [by_id[account_id] for account_id in dict.fromkeys(admitted_ids)]


def _load_regular_plan_accounts(
    session: Session,
    task: Task,
    facts: PlanFacts,
    account_limit: int,
) -> AccountPlanState | PlanAbort:
    accounts = _select_accounts_for_plan(
        session,
        task,
        facts.group,
        facts.hard_progress,
        facts.config,
        coverage_rows=facts.coverage.rows,
    )
    accounts = _online_ready_accounts(session, task, accounts, facts.hard_progress)
    accounts = accounts[:account_limit]
    if not accounts:
        _mark_account_shortage(session, task, facts)
        return PlanAbort()
    accounts, profiles, missing_ids = _profile_ready_accounts_for_plan(
        session,
        task,
        group=facts.group,
        progress=facts.hard_progress,
        config=facts.config,
        accounts=accounts,
        coverage_rows=facts.coverage.rows,
    )
    _expire_open_profileless_actions(session, task, profiles.keys())
    if missing_ids:
        _queue_missing_voice_profile_recovery(session, task, facts.config, missing_ids)
        _record_missing_voice_profiles(session, task, missing_ids)
    if accounts:
        return AccountPlanState(accounts)
    task.last_error = VOICE_PROFILE_MISSING_MESSAGE
    if facts.hard_progress:
        _mark_hard_blocked(task, facts.hard_progress, "voice_profile_missing")
    return PlanAbort()


def _load_daily_coverage_plan_accounts(
    session: Session,
    task: Task,
    facts: PlanFacts,
    account_limit: int,
    *,
    include_replan_accounts: bool,
) -> AccountPlanState | PlanAbort:
    if task.fulfillment_contract_version == "fact_first_v3":
        from ..task_group_bot_admission_recovery import reopen_unproven_task_coverages

        reopen_unproven_task_coverages(
            session,
            task,
            facts.group,
            limit=account_limit,
        )
    selected, admission_waiting, seen_account_ids = _initial_replan_daily_accounts(
        session, task, facts,
        account_limit=account_limit,
        include_replan_accounts=include_replan_accounts,
    )
    _scan_daily_coverage_accounts(
        session, task, facts, selected=selected,
        admission_waiting=admission_waiting,
        seen_account_ids=seen_account_ids, account_limit=account_limit,
    )
    selected.extend(
        _daily_group_extra_accounts(
            session,
            task,
            facts,
            selected=selected,
            account_limit=account_limit,
        )
    )
    if not selected and task.fulfillment_contract_version != "fact_first_v3":
        legacy_ready = _daily_voice_profile_ready_accounts(
            session,
            task,
            facts,
            admission_waiting,
        )
        selected.extend(legacy_ready[:account_limit])
    _record_group_bot_admission_waiting(task, admission_waiting)
    _record_direct_check_in_capacity(task, len(selected))
    if selected:
        return AccountPlanState(selected)
    if facts.coverage.required_new <= 0:
        _mark_daily_target_pacing(task)
        return PlanAbort()
    _mark_account_shortage(session, task, facts)
    return PlanAbort()


def _scan_daily_coverage_accounts(
    session: Session,
    task: Task,
    facts: PlanFacts,
    *,
    selected: list,
    admission_waiting: list,
    seen_account_ids: set[int],
    account_limit: int,
) -> None:
    page_limit = _daily_coverage_scan_page_limit()
    while len(selected) < account_limit:
        rows = ready_coverage_plan_batch(
            session, task, now=_now(), limit=page_limit,
            exclude_account_ids=seen_account_ids,
        ).rows
        if not rows:
            return
        seen_account_ids.update(int(row.account_id) for row in rows)
        ready, waiting = _daily_accounts_for_coverage_rows(
            session, task, facts, rows,
        )
        admission_waiting.extend(waiting)
        remaining = max(0, account_limit - len(selected))
        selected.extend(ready[:remaining])


def _initial_replan_daily_accounts(
    session: Session,
    task: Task,
    facts: PlanFacts,
    *,
    account_limit: int,
    include_replan_accounts: bool,
) -> tuple[list, list, set[int]]:
    rows = _replan_coverage_rows_for_plan(session, task, facts)
    seen = _bound_coverage_account_ids_for_plan(session, task, facts)
    if not rows:
        return [], [], seen
    if not include_replan_accounts:
        return [], [], seen
    ready, waiting = _daily_accounts_for_coverage_rows(
        session, task, facts, rows,
    )
    return ready[:account_limit], waiting, seen


def _bound_coverage_account_ids_for_plan(
    session: Session,
    task: Task,
    facts: PlanFacts,
) -> set[int]:
    target_id = str(facts.coverage.daily_group_target_id or "")
    target = session.get(TaskGroupDailyTarget, target_id) if target_id else None
    if target is None or not target.task_day_ledger_id:
        return set()
    statement = (
        select(TaskAccountDailyCoverage.account_id)
        .join(
            TaskGroupDailyMessageSlot,
            TaskGroupDailyMessageSlot.task_account_daily_coverage_id
            == TaskAccountDailyCoverage.id,
        )
        .join(
            ContentMixCycleSlot,
            ContentMixCycleSlot.primary_quantity_slot_id
            == TaskGroupDailyMessageSlot.id,
        )
        .join(ContentMixCycle, ContentMixCycle.id == ContentMixCycleSlot.cycle_id)
        .where(
            ContentMixCycle.task_id == task.id,
            ContentMixCycle.task_day_ledger_id == target.task_day_ledger_id,
        )
    )
    return {int(account_id) for account_id in session.scalars(statement)}


def _daily_accounts_for_coverage_rows(
    session: Session,
    task: Task,
    facts: PlanFacts,
    rows: list[TaskAccountDailyCoverage],
) -> tuple[list, list]:
    _include_daily_coverage_rows(facts.coverage, rows)
    accounts = _select_accounts_for_plan(
        session,
        task,
        facts.group,
        facts.hard_progress,
        facts.config,
        coverage_rows=rows,
    )
    accounts = _online_ready_accounts(
        session, task, accounts, facts.hard_progress,
    )
    admission_ready = _group_bot_ready_accounts_for_plan(
        session, task, facts.group, accounts,
    )
    admission_ready_ids = {account.id for account in admission_ready}
    waiting = [
        account for account in accounts if account.id not in admission_ready_ids
    ]
    ready = _daily_voice_profile_ready_accounts(
        session,
        task,
        facts,
        admission_ready,
    )
    return ready, waiting


def _daily_voice_profile_ready_accounts(
    session: Session,
    task: Task,
    facts: PlanFacts,
    accounts: list,
) -> list:
    if not accounts:
        return []
    if bool(facts.config.get("allow_mask_missing_check_in", False)):
        return accounts
    profiles = voice_profile_prompt_details(
        session,
        tenant_id=task.tenant_id,
        account_ids=[int(account.id) for account in accounts],
    )
    ready, missing_ids = _accounts_with_ready_voice_profiles(accounts, profiles)
    if not missing_ids:
        return ready
    next_retry_at = _queue_missing_voice_profile_recovery(
        session,
        task,
        facts.config,
        missing_ids,
    )
    block_voice_profile_coverage(
        session,
        task=task,
        account_ids=missing_ids,
        next_retry_at=next_retry_at,
        detail=VOICE_PROFILE_MISSING_MESSAGE,
        now=_now(),
    )
    _record_missing_voice_profiles(session, task, missing_ids)
    return ready


def _replan_coverage_rows_for_plan(
    session: Session,
    task: Task,
    facts: PlanFacts,
) -> list[TaskAccountDailyCoverage]:
    target_id = str(facts.coverage.daily_group_target_id or "")
    target = session.get(TaskGroupDailyTarget, target_id) if target_id else None
    if target is None or not target.task_day_ledger_id:
        return []
    fact_first = task.fulfillment_contract_version == "fact_first_v3"
    statement = _base_replan_coverage_statement(
        task,
        target.task_day_ledger_id,
        fact_first=fact_first,
    )
    if fact_first:
        statement = _prioritize_fact_first_replan_coverages(statement)
    else:
        statement = statement.order_by(
            ContentMixCycle.cycle_seq,
            ContentMixCycleSlot.slot_index,
        )
    return list(session.scalars(statement.limit(MAX_DAILY_COVERAGE_PLAN_BATCH)))


def _base_replan_coverage_statement(task: Task, ledger_id: str, *, fact_first: bool):
    statement = (
        select(TaskAccountDailyCoverage)
        .join(
            TaskGroupDailyMessageSlot,
            TaskGroupDailyMessageSlot.task_account_daily_coverage_id
            == TaskAccountDailyCoverage.id,
        )
        .join(
            ContentMixCycleSlot,
            ContentMixCycleSlot.primary_quantity_slot_id
            == TaskGroupDailyMessageSlot.id,
        )
        .join(ContentMixCycle, ContentMixCycle.id == ContentMixCycleSlot.cycle_id)
        .outerjoin(
            TaskGroupBotAdmission,
            and_(
                TaskGroupBotAdmission.task_id == task.id,
                TaskGroupBotAdmission.target_group_id
                == TaskAccountDailyCoverage.group_id,
                TaskGroupBotAdmission.account_id
                == TaskAccountDailyCoverage.account_id,
            ),
        )
        .where(
            ContentMixCycle.task_id == task.id,
            ContentMixCycle.task_day_ledger_id == ledger_id,
            ContentMixCycleSlot.slot_state.in_(
                {"unmaterialized", "replan_required"},
            ),
            TaskAccountDailyCoverage.state.in_(
                ("ready", "pending_admission")
                if fact_first
                else ("ready",)
            ),
            TaskAccountDailyCoverage.confirmed_count
            < TaskAccountDailyCoverage.target_count,
            has_no_terminal_shortfall_projection(),
        )
    )
    return statement


def _prioritize_fact_first_replan_coverages(statement):
    return (
        statement.where(or_(
            TaskGroupBotAdmission.id.is_(None),
            TaskGroupBotAdmission.state != "abandoned",
        )).order_by(
            _task_admission_plan_priority(),
            ContentMixCycle.cycle_seq,
            ContentMixCycleSlot.slot_index,
        )
    )


def _task_admission_plan_priority():
    return case(
        (TaskGroupBotAdmission.state == "ready", 0),
        (and_(
            TaskGroupBotAdmission.state == "observing",
            TaskGroupBotAdmission.no_prompt_pass_at <= _now(),
        ), 1),
        (TaskGroupBotAdmission.state == "requirements_pending", 2),
        (TaskGroupBotAdmission.state == "observing", 3),
        (TaskGroupBotAdmission.id.is_(None), 4),
        else_=5,
    )


def _daily_group_extra_accounts(
    session: Session,
    task: Task,
    facts: PlanFacts,
    *,
    selected: list,
    account_limit: int,
) -> list:
    extra_slot_count = _available_extra_quantity_slot_count(
        session, task, facts,
    )
    remaining = _daily_group_extra_account_limit(
        facts,
        selected_count=len(selected),
        account_limit=account_limit,
        extra_slot_count=extra_slot_count,
    )
    if remaining <= 0:
        return []
    candidate_ids = _daily_group_extra_candidate_ids(
        session, task, facts, selected,
    )
    if not candidate_ids:
        return []
    candidates = select_task_accounts(
        session,
        task.tenant_id,
        task.account_config or {},
        target_group_id=facts.group.id,
        limit=len(candidate_ids),
        enforce_max_concurrent=False,
        candidate_account_ids=candidate_ids,
    )
    selected_ids = {account.id for account in selected}
    candidates = [account for account in candidates if account.id not in selected_ids]
    candidates = _online_ready_accounts(session, task, candidates, {})
    candidates = _group_bot_ready_accounts_for_plan(
        session, task, facts.group, candidates,
    )
    candidates = _eligible_daily_group_extra_accounts(
        session,
        task,
        facts=facts,
        accounts=candidates,
    )
    counts = _daily_success_counts(session, task)
    return sorted(
        candidates,
        key=lambda account: (counts.get(account.id, 0), account.id),
    )[:remaining]


def _daily_group_extra_candidate_ids(
    session: Session,
    task: Task,
    facts: PlanFacts,
    selected: list,
) -> list[int]:
    target_id = str(facts.coverage.daily_group_target_id or "")
    target = session.get(TaskGroupDailyTarget, target_id) if target_id else None
    if target is None or not target.task_day_ledger_id:
        return []
    return daily_group_extra_candidate_ids(
        session,
        DailyGroupExtraCandidateSpec(
            tenant_id=task.tenant_id,
            task_id=task.id,
            group_id=facts.group.id,
            task_day_ledger_id=target.task_day_ledger_id,
            coverage_date=target.target_date,
            excluded_account_ids=frozenset(account.id for account in selected),
        ),
    )


def _eligible_daily_group_extra_accounts(
    session: Session,
    task: Task,
    *,
    facts: PlanFacts,
    accounts: list,
) -> list:
    target_id = str(facts.coverage.daily_group_target_id or "")
    target = session.get(TaskGroupDailyTarget, target_id) if target_id else None
    if target is None or not target.task_day_ledger_id or not accounts:
        return []
    confirmed_ids = set(session.scalars(
        select(TaskAccountDailyCoverage.account_id).where(
            TaskAccountDailyCoverage.tenant_id == task.tenant_id,
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.group_id == facts.group.id,
            TaskAccountDailyCoverage.task_day_ledger_id
            == target.task_day_ledger_id,
            TaskAccountDailyCoverage.state == "confirmed",
            TaskAccountDailyCoverage.confirmed_count
            >= TaskAccountDailyCoverage.target_count,
            TaskAccountDailyCoverage.account_id.in_(
                [int(account.id) for account in accounts]
            ),
        )
    ))
    covered_accounts = [
        account for account in accounts if int(account.id) in confirmed_ids
    ]
    if not covered_accounts:
        return []
    profiles = voice_profile_prompt_details(
        session,
        tenant_id=task.tenant_id,
        account_ids=[int(account.id) for account in covered_accounts],
    )
    ready_accounts, _missing_ids = _accounts_with_ready_voice_profiles(
        covered_accounts,
        profiles,
    )
    return ready_accounts


def _daily_group_extra_account_limit(
    facts: PlanFacts,
    *,
    selected_count: int,
    account_limit: int,
    extra_slot_count: int,
) -> int:
    needed = max(0, facts.coverage.volume_need_now - selected_count)
    return min(
        needed,
        max(0, account_limit - selected_count),
        max(0, extra_slot_count),
    )


def _available_extra_quantity_slot_count(
    session: Session,
    task: Task,
    facts: PlanFacts,
) -> int:
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        return max(0, int(facts.coverage.volume_need_now or 0))
    target_id = str(facts.coverage.daily_group_target_id or "")
    target = session.get(TaskGroupDailyTarget, target_id) if target_id else None
    if target is None or not target.task_day_ledger_id:
        return 0
    bound_quantity_slot = select(ContentMixCycleSlot.id).where(
        ContentMixCycleSlot.primary_quantity_slot_id
        == TaskGroupDailyMessageSlot.id,
    ).exists()
    statement = select(func.count(TaskGroupDailyMessageSlot.id)).where(
        TaskGroupDailyMessageSlot.task_id == task.id,
        TaskGroupDailyMessageSlot.task_day_ledger_id
        == target.task_day_ledger_id,
        TaskGroupDailyMessageSlot.slot_kind == "extra_volume",
        TaskGroupDailyMessageSlot.state == "open",
        TaskGroupDailyMessageSlot.task_account_daily_coverage_id.is_(None),
        ~bound_quantity_slot,
    )
    return int(session.scalar(statement) or 0)


def _group_bot_ready_accounts_for_plan(
    session: Session,
    task: Task,
    group: TgGroup,
    accounts: list,
) -> list:
    if not accounts:
        return []
    config = task.type_config if isinstance(task.type_config, dict) else {}
    required = _group_bot_admission_requirement(config)
    if required is False:
        return accounts
    if task.fulfillment_contract_version == "fact_first_v3":
        abandoned_ids = set(session.scalars(select(TaskGroupBotAdmission.account_id).where(
            TaskGroupBotAdmission.task_id == task.id,
            TaskGroupBotAdmission.target_group_id == group.id,
            TaskGroupBotAdmission.state == "abandoned",
        )))
        return [account for account in accounts if account.id not in abandoned_ids]
    admissions = session.scalars(select(GroupBotAdmission).where(
        GroupBotAdmission.tenant_id == task.tenant_id,
        GroupBotAdmission.group_id == group.id,
        GroupBotAdmission.account_id.in_([account.id for account in accounts]),
    ))
    admission_rows = list(admissions)
    state_by_account = {
        int(admission.account_id): str(admission.state or "")
        for admission in admission_rows
    }
    plannable_ids = plannable_admission_account_ids(session, admission_rows)
    return [
        account for account in accounts
        if int(account.id) in plannable_ids
        or (required is not True and int(account.id) not in state_by_account)
    ]


def _daily_success_counts(session: Session, task: Task) -> dict[int, int]:
    start = datetime.combine(_now().date(), time.min)
    rows = session.execute(
        select(Action.account_id, func.count(Action.id))
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.action_type == "send_message",
            Action.status == "success",
            Action.executed_at >= start,
            Action.account_id.is_not(None),
        )
        .group_by(Action.account_id)
    )
    return {int(account_id): int(count) for account_id, count in rows}


def _daily_coverage_scan_page_limit() -> int:
    configured = int(get_settings().daily_coverage_plan_batch_limit or 1)
    return max(1, configured)


def _include_daily_coverage_rows(
    coverage: CoveragePlanState,
    rows: list[TaskAccountDailyCoverage],
) -> None:
    known_ids = {row.id for row in coverage.rows}
    coverage.rows.extend(row for row in rows if row.id not in known_ids)
    coverage.rows_by_account.update({int(row.account_id): row for row in rows})


def _record_direct_check_in_capacity(task: Task, ready_count: int) -> None:
    stats = dict(task.stats or {})
    stats["direct_check_in_ready_account_count"] = ready_count
    task.stats = stats


def _record_group_bot_admission_waiting(task: Task, accounts: list) -> None:
    waiting_count = len({int(account.id) for account in accounts})
    stats = dict(task.stats or {})
    stats["pending_group_bot_admission_count"] = waiting_count
    if waiting_count:
        stats["skip_reason"] = "pending_group_bot_admission"
    elif stats.get("skip_reason") == "pending_group_bot_admission":
        stats.pop("skip_reason", None)
    task.stats = stats


def _mark_account_shortage(session: Session, task: Task, facts: PlanFacts) -> None:
    error_message, reason = _account_shortage_reason(
        session, task, facts.group, facts.hard_progress, config=facts.config,
    )
    if int((task.stats or {}).get("account_offline_count") or 0) > 0:
        error_message = "账号在线状态不可用，等待账号恢复在线后继续执行"
        reason = "account_offline"
    task.last_error = error_message
    if facts.hard_progress:
        deficit = max(1, int(facts.hard_progress.get("deficit") or 1))
        mark_plan_result(task, facts.hard_progress, 0, {reason: deficit})


def _load_context_plan(
    session: Session, task: Task, facts: PlanFacts,
) -> ContextPlanState | PlanAbort:
    depth = int(facts.config.get("chat_history_depth") or 50)
    fingerprint_source = f"{task.id}:group_ai_chat:{facts.group.id}"
    history_rows = recent_context_messages(session, facts.group, depth)
    context_rows = list(reversed(history_rows[-depth:]))
    usable_rows = _topic_relevant_context_rows(
        facts.config,
        [row for row in context_rows if _is_human_context_row(row) and _is_usable_context_message(row.content)],
    )
    unprocessed_rows = [
        row
        for row in usable_rows
        if not fingerprint_exists(
            session, task.tenant_id, fingerprint_source, _context_fingerprint(row),
        )
    ]
    mode, ramp_ratio = ai_cycle_mode(facts.config, task.scheduled_start)
    previous = _recent_ai_messages(
        session, task, limit=_semantic_repeat_window(facts.config),
    )
    idle = _resolve_idle_continuation(
        session,
        task,
        facts,
        usable_rows=usable_rows,
        unprocessed_rows=unprocessed_rows,
        mode=mode,
        ramp_ratio=ramp_ratio,
    )
    if isinstance(idle, PlanAbort):
        return idle
    return ContextPlanState(
        depth, fingerprint_source, usable_rows, unprocessed_rows, previous, mode, ramp_ratio, idle,
    )


def _resolve_idle_continuation(
    session: Session,
    task: Task,
    facts: PlanFacts,
    *,
    usable_rows: list[Any],
    unprocessed_rows: list[Any],
    mode: str,
    ramp_ratio: float,
) -> bool | PlanAbort:
    force_bootstrap = bool((task.stats or {}).get("force_bootstrap_once"))
    should_wait = (
        not facts.hard_progress
        and facts.coverage.required_new <= 0
        and not force_bootstrap
        and _should_wait_for_human_context(session, task, usable_rows, unprocessed_rows)
    )
    if not should_wait:
        return False
    decision = _idle_continuation_decision(session, task, facts.config)
    if decision["due"]:
        return True
    _mark_waiting_context(
        task,
        facts.config,
        mode,
        ramp_ratio,
        context_mode="waiting_new_context",
        next_run_at=decision["next_run_at"],
    )
    return PlanAbort()


def _load_turn_plan(
    session: Session,
    task: Task,
    facts: PlanFacts,
    *,
    accounts: list[Any],
    context: ContextPlanState,
) -> TurnPlanState | PlanAbort:
    cycle_index = _next_cycle_index(session, task)
    if facts.continuity_reply_targets:
        return _continuity_turn_plan(facts, accounts, context, cycle_index)
    bounded_daily_coverage_batch = (
        task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION
        and _all_accounts_daily_coverage(facts.config)
    )
    round_config = _coverage_round_config(
        _hard_hourly_round_config(facts.config, facts.hard_progress),
        facts.hard_progress,
    )
    selected, turn_count = _select_turn_accounts(
        session,
        task,
        facts,
        accounts=accounts,
        context=context,
        cycle_index=cycle_index,
        round_config=round_config,
        bounded_daily_coverage_batch=bounded_daily_coverage_batch,
    )
    if not selected or turn_count <= 0:
        _mark_daily_target_pacing(task)
        return PlanAbort()
    turn_count = _limited_turn_count(
        session,
        task,
        facts,
        context=context,
        cycle_index=cycle_index,
        turn_count=turn_count,
        bounded_daily_coverage_batch=bounded_daily_coverage_batch,
    )
    if turn_count <= 0:
        task.last_error = "已有待执行消息占满上下文有效窗口，等待现有消息执行后继续规划"
        return PlanAbort()
    selected = selected[: min(len(selected), turn_count)]
    history = _context_plan_history(facts, context)
    return TurnPlanState(cycle_index, round_config, selected, turn_count, history)


def _continuity_turn_plan(
    facts: PlanFacts,
    accounts: list[Any],
    context: ContextPlanState,
    cycle_index: int,
) -> TurnPlanState:
    turn_count = len(facts.continuity_reply_targets)
    selected = accounts[: min(len(accounts), turn_count)]
    round_config = _coverage_round_config(
        _hard_hourly_round_config(facts.config, facts.hard_progress),
        facts.hard_progress,
    )
    return TurnPlanState(
        cycle_index, {**round_config, "allow_account_repeat": True},
        selected, turn_count, _context_plan_history(facts, context),
    )


def _select_turn_accounts(
    session: Session,
    task: Task,
    facts: PlanFacts,
    *,
    accounts: list[Any],
    context: ContextPlanState,
    cycle_index: int,
    round_config: dict,
    bounded_daily_coverage_batch: bool,
) -> tuple[list[Any], int]:
    return _select_cycle_accounts(
        accounts,
        round_config,
        context.mode,
        context.ramp_ratio,
        has_context=bool(context.usable_rows),
        cycle_index=cycle_index,
        pacing_config=task.pacing_config or {},
        daily_coverage_uncovered_count=_turn_daily_uncovered_count(
            session, task, facts, accounts=accounts,
        ),
        bounded_daily_coverage_batch=bounded_daily_coverage_batch,
    )


def _limited_turn_count(
    session: Session,
    task: Task,
    facts: PlanFacts,
    *,
    context: ContextPlanState,
    cycle_index: int,
    turn_count: int,
    bounded_daily_coverage_batch: bool,
) -> int:
    deferred = _daily_coverage_generation_is_deferred(
        session,
        task,
        facts.group,
        usable_context_rows=context.usable_rows,
        turn_count=turn_count,
        config=facts.config,
        hard_progress=facts.hard_progress,
        has_daily_coverage_debt=facts.coverage.required_new > 0,
    )
    times = _schedule_times_for_plan(
        session,
        task,
        facts.hard_progress,
        turn_count,
        mode=context.mode,
        deadline_at=facts.coverage.deadline_at,
        slot_keys=_turn_slot_keys(task, cycle_index, turn_count),
    )
    limited, _times = _limit_context_bound_turns(
        task,
        facts.config,
        has_context=bool(context.usable_rows),
        progress=facts.hard_progress,
        deferred_generation=(
            deferred and task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION
        ),
        preserve_daily_coverage_batch=bounded_daily_coverage_batch,
        turn_count=turn_count,
        planned_times=times,
    )
    return limited


def _mark_daily_target_pacing(task: Task) -> None:
    stats = dict(task.stats or {})
    stats["skip_reason"] = "daily_target_pacing"
    task.stats = stats
    task.last_error = "群日目标按计划推进中，等待下一发送时点"


def _turn_daily_uncovered_count(
    session: Session, task: Task, facts: PlanFacts, *, accounts: list[Any],
) -> int:
    return _daily_coverage_uncovered_count(
        session,
        task,
        accounts,
        facts.hard_progress,
        facts.config,
        coverage_state=facts.coverage,
    )


def _context_plan_history(facts: PlanFacts, context: ContextPlanState) -> str:
    parts = [f"{row.sender_name}: {row.content}" for row in context.usable_rows[-50:]]
    if context.idle_continuation:
        parts.append(
            _idle_continuation_history(
                facts.config, facts.group, context.previous_ai_messages,
            ),
        )
    elif not context.usable_rows:
        parts.append(_bootstrap_history(facts.config, facts.group))
    return "\n".join(parts)


def _input_filter_abort(task: Task, facts: PlanFacts, history: str) -> bool:
    result = evaluate_input_filter(
        history,
        message_type="text",
        filters=facts.rule_version.filters or {},
    )
    if result.passed:
        return False
    task.last_error = f"规则输入过滤跳过：{result.reason}"
    stats_inc(task, "skipped_count")
    if facts.hard_progress:
        _mark_hard_blocked(task, facts.hard_progress, "input_filter")
    return True


def _load_profile_plan(
    session: Session,
    task: Task,
    facts: PlanFacts,
    *,
    context: ContextPlanState,
    turn: TurnPlanState,
) -> ProfilePlanState:
    account_ids = [account.id for account in turn.selected]
    topic_thread = _topic_thread_summary(
        facts.config, facts.group, context.usable_rows, context.previous_ai_messages,
    )
    topic_plan = _topic_plan_summary(
        facts.config, facts.group, topic_thread, turn.turn_count,
    )
    memories = _recent_account_memories(
        session,
        task,
        account_ids,
        group_id=facts.group.id,
        depth=int(facts.config.get("account_memory_depth") or 3),
    )
    account_profiles = account_profile_summaries(
        session,
        task,
        account_ids,
        group_id=facts.group.id,
    )
    voices = voice_profile_prompt_details(
        session, tenant_id=task.tenant_id, account_ids=account_ids,
    )
    stances = group_stance_summaries(
        session, tenant_id=task.tenant_id, group_id=facts.group.id, account_ids=account_ids,
    )
    preview = tenant_learning_profile_preview(session, task.tenant_id, GROUP_CHAT_SCENE)
    audit_learning_profile_use(session, task, preview, "AI活群任务")
    prompt_profiles = _account_prompt_profiles(account_profiles, voices, stances)
    counts = _coverage_counts_for_plan(
        turn.selected, turn.round_config, facts.coverage.rows_by_account,
    )
    rows = _coverage_rows_for_plan(
        turn.selected, turn.round_config, facts.coverage.rows_by_account,
    )
    selected = _prioritize_accounts_for_plan(
        turn.selected, memories, counts, turn.round_config,
    )
    return ProfilePlanState(
        selected, topic_thread, topic_plan, memories, prompt_profiles, voices, stances,
        preview, counts, rows, f"{task.id}:cycle:{turn.cycle_index}",
    )


def _load_generation_plan(
    session: Session,
    task: Task,
    facts: PlanFacts,
    *,
    context: ContextPlanState,
    turn: TurnPlanState,
    profile: ProfilePlanState,
) -> GenerationPlanState | PlanAbort:
    reply_targets, coverage_reply_shortfall = _reply_targets_for_plan(
        session,
        task,
        facts.group,
        context.usable_rows,
        turn.turn_count,
        facts.config,
        facts.hard_progress,
        daily_coverage_debt=facts.coverage.required_new > 0,
        daily_group_target_id=facts.coverage.daily_group_target_id,
    )
    if reply_targets is None:
        return PlanAbort()
    quality_items, burst_plan, is_generic_warmup = _generation_quality_items(
        turn,
        profile,
        reply_targets,
        has_context=bool(context.usable_rows),
    )
    chat_mode = _chat_mode(context.usable_rows, context.idle_continuation)
    if not quality_items:
        _mark_empty_generation_plan(task, facts, context, chat_mode=chat_mode)
        return PlanAbort()
    schedule = _finalize_generation_schedule(
        session, task, facts, context, quality_items=quality_items,
        is_generic_warmup=is_generic_warmup,
    )
    if schedule is None:
        return PlanAbort()
    return _generation_plan_state(
        schedule,
        context=context,
        coverage_reply_shortfall=coverage_reply_shortfall,
        burst_plan=burst_plan,
        chat_mode=chat_mode,
    )


def _generation_quality_items(
    turn: TurnPlanState,
    profile: ProfilePlanState,
    reply_targets: list[dict],
    *,
    has_context: bool,
) -> tuple[list[dict], dict, bool]:
    allow_repeat = bool(turn.round_config.get("allow_account_repeat", True))
    burst_plan = _consecutive_burst_plan(
        turn.round_config, turn.turn_count, allow_repeat, profile.cycle_id,
    )
    is_generic_warmup = not has_context and not any(reply_targets)
    slots = _immutable_generation_slots(
        turn,
        profile,
        reply_targets=reply_targets,
        allow_repeat=allow_repeat,
        burst_plan=burst_plan,
        is_generic_warmup=is_generic_warmup,
    )
    normal_count = max(0, turn.turn_count - len(reply_targets))
    planned_items = _deferred_ai_planned_items(reply_targets, normal_count, slots)
    return planned_items[:turn.turn_count], burst_plan, is_generic_warmup


def _generation_plan_state(
    schedule: tuple[list[dict], list[datetime]],
    *,
    context: ContextPlanState,
    coverage_reply_shortfall: int,
    burst_plan: dict,
    chat_mode: str,
) -> GenerationPlanState:
    quality_items, times = schedule
    requested_reply_count = sum(
        1 for item in quality_items if item.get("reply_target")
    )
    message_ids = [int(row.id) for row in context.usable_rows[-context.history_depth:]]
    source = _generation_source(context.usable_rows, context.idle_continuation)
    return GenerationPlanState(
        quality_items,
        times,
        requested_reply_count,
        coverage_reply_shortfall,
        burst_plan,
        chat_mode,
        message_ids,
        source,
    )


def _finalize_generation_schedule(
    session: Session,
    task: Task,
    facts: PlanFacts,
    context: ContextPlanState,
    *,
    quality_items: list[dict],
    is_generic_warmup: bool,
) -> tuple[list[dict], list[datetime]] | None:
    requested_count = len(quality_items)
    quality_items, times = _schedule_generation_items(
        session,
        task,
        facts,
        context,
        quality_items=quality_items,
        is_generic_warmup=is_generic_warmup,
    )
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION and len(times) < requested_count:
        if task.last_error.startswith("generic_warmup_"):
            _record_generic_warmup_shortfall(task, requested_count)
            return None
        _record_ai_pacing_shortfall(task, requested_count, len(times))
        return None
    quality_items, times = _limit_context_bound_quality_schedule(
        task,
        facts.config,
        has_context=bool(context.usable_rows),
        progress=facts.hard_progress,
        deferred_generation=True,
        context_bound_reply_only=(
            task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION
            and _all_accounts_daily_coverage(facts.config)
        ),
        quality_items=quality_items,
        planned_times=times,
    )
    if quality_items:
        return quality_items, times
    task.last_error = "上下文绑定计划超出有效发送窗口，等待新上下文后继续执行"
    stats_inc(task, "skipped_count")
    return None


def _schedule_generation_items(
    session: Session,
    task: Task,
    facts: PlanFacts,
    context: ContextPlanState,
    *,
    quality_items: list[dict],
    is_generic_warmup: bool,
) -> tuple[list[dict], list[datetime]]:
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        return _current_ai_pacing_schedule(
            session,
            task,
            facts,
            quality_items,
            is_generic_warmup=is_generic_warmup,
        )
    times = _schedule_times_for_plan(
        session,
        task,
        facts.hard_progress,
        len(quality_items),
        mode=context.mode,
        deadline_at=facts.coverage.deadline_at,
    )
    return quality_items, times


def _current_ai_pacing_schedule(
    session: Session,
    task: Task,
    facts: PlanFacts,
    quality_items: list[dict],
    *,
    is_generic_warmup: bool,
) -> tuple[list[dict], list[datetime]]:
    from ..engagement_attention import apply_proactive_quiet_windows

    quality_items = apply_proactive_quiet_windows(
        session,
        task,
        facts.group,
        facts.config,
        quality_items,
    )
    assignments, points_by_slot, capacity_slots = _ai_source_capacity_schedule(
        session,
        task,
        facts,
        quality_items,
    )
    quality_items = _freeze_scheduled_content_intents(
        session,
        task,
        facts,
        assignments,
        quality_items,
        is_generic_warmup=is_generic_warmup,
    )
    if quality_items is None:
        return [], []
    return _materialize_ai_pacing_schedule(
        task, assignments, quality_items, points_by_slot, capacity_slots
    )


def _ai_source_capacity_schedule(
    session: Session,
    task: Task,
    facts: PlanFacts,
    quality_items: list[dict],
) -> tuple[list[AiPacingAssignment], dict, list[SourcePacingSlot]]:
    assignments = assign_ai_pacing_slots(
        session,
        task,
        daily_group_target_id=facts.coverage.daily_group_target_id,
        effective_plan_total=facts.coverage.effective_daily_target,
        coverage_by_account=facts.coverage.rows_by_account,
        item_account_ids=[_quality_slot_account_id(item) for item in quality_items],
        item_continuity_claim_ids=[
            _reply_target_text(item, "conversation_turn_claim_id")
            for item in quality_items
        ],
    )
    points_by_slot = schedule_source_pacing_points(
        [item.source_slot for item in assignments],
        task.pacing_config or {},
        seed_id=f"ai:{task.id}",
        now_at=wall_datetime(_now()),
        timezone_name=task.timezone,
    )
    points_by_slot, capacity_slots = apply_source_capacity_plan(
        session,
        task,
        [item.source_slot for item in assignments],
        points=points_by_slot,
        pacing_domain="ai_send",
    )
    assignments = [
        assignment
        for assignment in assignments
        if assignment.source_slot.slot_key in points_by_slot
    ]
    return assignments, points_by_slot, capacity_slots


def _freeze_scheduled_content_intents(
    session: Session,
    task: Task,
    facts: PlanFacts,
    assignments: list[AiPacingAssignment],
    quality_items: list[dict],
    *,
    is_generic_warmup: bool,
) -> list[dict] | None:
    target_id = facts.target.id if facts.target else int(
        facts.config.get("target_operation_target_id") or 0
    )
    if not assignments or facts.config.get("topic_participation_rate") is None:
        return quality_items
    if not target_id:
        raise ValueError("ai_group_content_target_missing")
    try:
        return freeze_content_intents(
            session,
            task,
            daily_group_target_id=facts.coverage.daily_group_target_id,
            target_operation_target_id=target_id,
            canonical_group_id=facts.group.id,
            assignments=assignments,
            quality_items=quality_items,
            config_revision=facts.task_config_revision,
            is_generic_warmup=is_generic_warmup,
        )
    except GenericWarmupQuestionWait as exc:
        task.last_error = str(exc)
        return None


def _materialize_ai_pacing_schedule(
    task: Task,
    assignments: list[AiPacingAssignment],
    quality_items: list[dict],
    points_by_slot: dict,
    capacity_slots: list[SourcePacingSlot],
) -> tuple[list[dict], list[datetime]]:
    capacity_by_key = {slot.slot_key: slot for slot in capacity_slots}
    enriched: list[dict] = []
    due_times: list[datetime] = []
    for assignment in assignments:
        point = points_by_slot.get(assignment.source_slot.slot_key)
        if point is None:
            continue
        item = quality_items[assignment.item_index]
        timing = _ai_assignment_timing(item, assignment, point)
        if timing is None:
            continue
        due_at, release_at, deadline_at, response_reflowed = timing
        _freeze_ai_pacing_assignment(
            task,
            assignment,
            due_at,
            release_at,
            capacity_slot=(
                None
                if response_reflowed
                else capacity_by_key.get(assignment.source_slot.slot_key)
            ),
        )
        enriched.append({
            **item,
            "pacing_quantity_slot_id": assignment.owner.id,
            "pacing_slot_key": assignment.source_slot.slot_key,
            "pacing_deadline_at": deadline_at,
            "response_reflowed": response_reflowed,
        })
        due_times.append(release_at)
    return enriched, due_times


def _ai_assignment_timing(
    item: dict,
    assignment: AiPacingAssignment,
    point,
) -> tuple[datetime, datetime, datetime, bool] | None:
    target = item.get("reply_target") if isinstance(item, dict) else None
    source_deadline = wall_datetime(assignment.source_slot.deadline_at)
    if not isinstance(target, dict) or not target.get("conversation_turn_claim_id"):
        quiet_until = _reply_target_datetime(item, "proactive_quiet_until_at")
        release_at = latest_wall_datetime(
            wall_datetime(point.release_not_before_at),
            quiet_until or wall_datetime(point.release_not_before_at),
        )
        if release_at >= source_deadline:
            return None
        return point.due_at, release_at, source_deadline, False
    natural_at = _reply_target_datetime(target, "response_not_before_at")
    freshness_at = _reply_target_datetime(target, "freshness_deadline_at")
    if natural_at is None or freshness_at is None:
        return None
    deadline_at = min(source_deadline, freshness_at)
    owner = assignment.owner
    if owner.pacing_due_at is None:
        call_at = latest_wall_datetime(wall_datetime(_now()), natural_at)
        return (call_at, call_at, deadline_at, True) if call_at < deadline_at else None
    due_at = wall_datetime(owner.pacing_due_at)
    release_at = latest_wall_datetime(
        wall_datetime(_now()),
        natural_at,
        wall_datetime(owner.release_not_before_at or owner.pacing_due_at),
    )
    if release_at >= deadline_at:
        return None
    return due_at, release_at, deadline_at, release_at != point.release_not_before_at


def _reply_target_datetime(target: dict, key: str) -> datetime | None:
    raw = target.get(key)
    if isinstance(raw, datetime):
        return wall_datetime(raw)
    try:
        return wall_datetime(datetime.fromisoformat(str(raw))) if raw else None
    except ValueError:
        return None


def _freeze_ai_pacing_assignment(
    task: Task,
    assignment: AiPacingAssignment,
    due_at: datetime,
    release_not_before_at: datetime,
    *,
    capacity_slot: SourcePacingSlot | None = None,
) -> None:
    source = assignment.source_slot
    config = task.pacing_config or {}
    seed_id = f"ai:{task.id}"
    _replace_stale_released_owner_release(
        assignment.owner,
        release_not_before_at,
    )
    freeze_pacing_owner(
        assignment.owner,
        plan_hash=source_pacing_plan_hash(
            source,
            config,
            seed_id=seed_id,
        ),
        slot_ordinal=source.slot_ordinal,
        plan_total=source.plan_total,
        due_at=due_at,
        release_not_before_at=release_not_before_at,
        source_identity=source.owner_identity,
        previous_plan_hash=_previous_ai_plan_hash(
            assignment,
            source,
            config=config,
            seed_id=seed_id,
        ),
    )
    if capacity_slot and capacity_slot.source_capacity_plan_hash:
        assignment.owner.source_capacity_plan_hash = capacity_slot.source_capacity_plan_hash
        assignment.owner.source_capacity_slot_ordinal = capacity_slot.source_capacity_slot_ordinal


def _replace_stale_released_owner_release(
    owner: TaskGroupDailyMessageSlot,
    proposed: datetime,
) -> None:
    current = owner.release_not_before_at
    if owner.task_lifecycle_epoch is not None or current is None:
        return
    if wall_datetime(current) < wall_datetime(proposed):
        owner.release_not_before_at = None


def _previous_ai_plan_hash(
    assignment: AiPacingAssignment,
    source: SourcePacingSlot,
    *,
    config: dict,
    seed_id: str,
) -> str | None:
    previous_total = int(assignment.owner.pacing_plan_total or 0)
    if previous_total <= 0 or previous_total >= source.plan_total:
        return None
    previous_source = replace(source, plan_total=previous_total)
    return source_pacing_plan_hash(previous_source, config, seed_id=seed_id)


def _immutable_generation_slots(
    turn: TurnPlanState,
    profile: ProfilePlanState,
    *,
    reply_targets: list[dict],
    allow_repeat: bool,
    burst_plan: dict,
    is_generic_warmup: bool,
) -> list[dict]:
    burst_account = (
        _slot_account(profile.selected, min(burst_plan), allow_repeat) if burst_plan else None
    )
    usage = _conversation_target_usage_config(turn.round_config)
    return _generation_slots_for_plan(
        cycle_id=profile.cycle_id,
        accounts=profile.selected,
        turn_count=turn.turn_count,
        reply_targets=reply_targets,
        account_prompt_profiles=profile.account_prompt_profiles,
        allow_account_repeat=allow_repeat,
        burst_plan=burst_plan,
        burst_account=burst_account,
        topic_directions=(
            []
            if turn.round_config.get("topic_participation_rate") is not None
            else _slot_topic_directions(turn.round_config)
        ),
        teacher_targets=_slot_teacher_targets(turn.round_config),
        recent_topic_counts=usage.get("topics", {}),
        recent_teacher_counts=usage.get("teachers", {}),
        is_generic_warmup=is_generic_warmup,
    )


def _mark_empty_generation_plan(
    task: Task, facts: PlanFacts, context: ContextPlanState, *, chat_mode: str,
) -> None:
    _mark_quality_skip(
        task,
        facts.config,
        context.mode,
        context.ramp_ratio,
        _context_mode(context.usable_rows, context.idle_continuation),
        chat_mode,
        {},
    )
    if facts.hard_progress:
        deficit = int(facts.hard_progress.get("deficit") or 1)
        mark_plan_result(task, facts.hard_progress, 0, {"quality_filter": deficit})


def _prepare_plan_blueprint(
    session: Session,
    task: Task,
    *,
    include_replan_accounts: bool = True,
) -> PlanBlueprint | PlanAbort:
    facts = _load_plan_facts(session, task)
    if isinstance(facts, PlanAbort):
        return facts
    account_state = _load_plan_accounts(
        session,
        task,
        facts,
        include_replan_accounts=include_replan_accounts,
    )
    if isinstance(account_state, PlanAbort):
        return account_state
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        from ..task_group_bot_admission_v2 import ensure_task_admission_observation

        for account in account_state.accounts:
            ensure_task_admission_observation(
                session,
                task_id=task.id,
                tenant_id=task.tenant_id,
                group_id=facts.group.id,
                account_id=int(account.id),
            )
    context = _load_context_plan(session, task, facts)
    if isinstance(context, PlanAbort):
        return context
    turn = _load_turn_plan(
        session, task, facts, accounts=account_state.accounts, context=context,
    )
    if isinstance(turn, PlanAbort):
        return turn
    if _input_filter_abort(task, facts, turn.history):
        return PlanAbort()
    profile = _load_profile_plan(session, task, facts, context=context, turn=turn)
    generation = _load_generation_plan(
        session, task, facts, context=context, turn=turn, profile=profile,
    )
    if isinstance(generation, PlanAbort):
        return generation
    return PlanBlueprint(facts, context, turn, profile, generation)


def _build_slot_snapshot(slot: SlotBuildInput) -> SlotSnapshot:
    payload_data: dict[str, Any] = {}
    for fields in (
        _slot_identity_payload(slot),
        _slot_profile_payload(slot),
        _slot_allocation_payload(slot),
        _slot_conversation_payload(slot),
        _slot_generation_payload(slot),
        _slot_rule_payload(slot),
    ):
        payload_data.update(fields)
    blueprint = slot.blueprint
    payload_data.update(blueprint.generation.burst_plan.get(slot.index, {}))
    payload_data.update(
        _coverage_payload_for_account(
            blueprint.turn.round_config,
            slot.account.id,
            blueprint.profile.coverage_counts,
            blueprint.profile.coverage_rows,
        ),
    )
    _apply_mask_content_contract(payload_data, slot)
    return SlotSnapshot(
        account_id=slot.account.id,
        planned_at=slot.planned_at,
        payload=SendMessagePayload(**payload_data),
    )


def _with_content_variation_key(snapshot: SlotSnapshot) -> SlotSnapshot:
    payload = snapshot.payload
    if not payload.coverage_ledger_id:
        return snapshot
    variation_key, context_version = _content_variation_identity(
        payload,
        account_id=snapshot.account_id,
    )
    updated = payload.model_copy(update={
        "content_variation_key": variation_key,
        "content_context_version": context_version,
    })
    return replace(snapshot, payload=updated)


def _content_variation_identity(
    payload: SendMessagePayload,
    *,
    account_id: int,
) -> tuple[str, str]:
    source = {
        "coverage_ledger_id": payload.coverage_ledger_id,
        "target_reference_revision": payload.target_reference_revision,
        "coverage_window_date": payload.coverage_window_date,
        "account_id": account_id,
        "primary_quantity_slot_id": payload.primary_quantity_slot_id,
        "fallback_obligation_key": payload.fallback_obligation_key,
        "topic_direction": payload.topic_direction,
        "teacher_target": payload.teacher_target,
        "act_type": payload.act_type,
        "relation_kind": payload.relation_kind,
        "material_intent": payload.material_intent,
        "reply_to_message_id": payload.reply_to_message_id,
        "context_message_ids": payload.context_message_ids,
    }
    encoded = json.dumps(source, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    context_version = hashlib.sha256(
        json.dumps(payload.context_message_ids, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:24]
    return hashlib.sha256(encoded).hexdigest(), context_version


def _slot_identity_payload(slot: SlotBuildInput) -> dict[str, Any]:
    facts = slot.blueprint.facts
    profile = slot.blueprint.profile
    item = slot.item
    account_id = slot.account.id
    target = facts.target
    target_id = target.id if target else int(facts.config.get("target_operation_target_id") or 0) or None
    target_revision = int(target.reference_revision or 1) if target else int(facts.config.get("target_reference_revision") or 0) or None
    target_snapshot = {
        "tg_peer_id": str(target.tg_peer_id if target else facts.group.tg_peer_id),
        "username": str(target.username if target else ""),
        "title": str(target.title if target else facts.target_label),
    }
    return {
        "chat_id": facts.group.tg_peer_id,
        "group_id": facts.group.id,
        "operation_target_id": target_id,
        "target_operation_target_id": target_id,
        "target_reference_revision": target_revision,
        "target_reference_snapshot": target_snapshot,
        "task_config_revision": facts.task_config_revision,
        "primary_quantity_slot_id": str(item.get("pacing_quantity_slot_id") or ""),
        "target_display": facts.target_label,
        "content_scope_contract_version": "group_content_scope_v1",
        "content_scope_tenant_id": facts.group.tenant_id,
        "content_scope_group_id": facts.group.id,
        "content_scope_task_id": facts.task_id,
        "message_text": "",
        "media_segments": [],
        "review_approved": True,
        "cycle_id": profile.cycle_id,
        "turn_index": slot.index + 1,
        "account_role": _role_for_account(account_id, slot.index, facts.config),
        "account_memory": profile.account_memories.get(str(account_id), ""),
        "account_profile": profile.account_prompt_profiles.get(str(account_id), ""),
        "slot_id": _quality_slot_id(item) or _slot_id(profile.cycle_id, slot.index),
        "act_type": _act_type_for_turn(slot.index, item),
    }


def _slot_profile_payload(slot: SlotBuildInput) -> dict[str, Any]:
    profile = slot.blueprint.profile
    item = slot.item
    voice = profile.voice_profiles.get(slot.account.id, {})
    summary = str(voice.get("summary") or "")
    version = int(voice.get("version") or 0)
    mask_id = str(voice.get("id") or "")
    snapshot_hash = str(voice.get("snapshot_hash") or "")
    contract_version = str(voice.get("contract_version") or "")
    return {
        "account_voice_profile_version": version,
        "account_voice_profile_summary": summary,
        "account_voice_profile_match_score": VOICE_PROFILE_MATCH_SCORE,
        "account_voice_profile_match_reason": "deferred_ai_generation",
        "account_mask_version": version,
        "account_mask_id": mask_id,
        "account_mask_snapshot_hash": snapshot_hash,
        "account_mask_summary": summary,
        "account_mask_match_score": VOICE_PROFILE_MATCH_SCORE,
        "account_mask_match_reason": "deferred_ai_generation",
        "voice_profile_contract_version": contract_version,
        "stance_summary": profile.stance_summaries.get(slot.account.id, ""),
        "ai_message_memory_id": "",
        "rewrite_attempts": int(item.get("rewrite_attempts") or 0),
        "human_quality_decision": str(item.get("human_quality_decision") or "accepted"),
        "quality_fallback": str(item.get("quality_fallback") or ""),
        "topic_direction": _quality_topic_direction(item, slot.blueprint.facts.config),
        "teacher_target": _quality_teacher_target(item, slot.blueprint.facts.config),
    }


def _slot_allocation_payload(slot: SlotBuildInput) -> dict[str, Any]:
    frozen = _quality_slot(slot.item)
    payload = {
        "allocation_plan_id": str(frozen.get("allocation_plan_id") or ""),
        "content_intent_id": str(frozen.get("content_intent_id") or ""),
        "content_intent_config_revision": int(
            frozen.get("content_intent_config_revision") or 0
        ),
        "content_intent_config_snapshot_hash": str(
            frozen.get("content_intent_config_snapshot_hash") or ""
        ),
        "content_intent_task_lifecycle_epoch": int(
            frozen.get("content_intent_task_lifecycle_epoch") or 0
        ),
        "content_intent_target_reference_revision": int(
            frozen.get("content_intent_target_reference_revision") or 0
        ),
        "content_contract_revision": str(frozen.get("content_contract_revision") or ""),
        "normal_text_ordinal": int(frozen.get("normal_text_ordinal") or 0),
        "relation_kind": str(frozen.get("relation_kind") or ""),
        "act_type": str(frozen.get("act_type") or ""),
        "content_intent_stance": str(frozen.get("stance") or ""),
        "topic_rate_bps": int(frozen.get("topic_rate_bps") or 0),
        "topic_budget_eligible": bool(frozen.get("topic_budget_eligible")),
        "topic_mode": str(frozen.get("topic_mode") or ""),
        "topic_capacity_reservation_id": str(
            frozen.get("topic_capacity_reservation_id") or ""
        ),
        "surface_scope_key": str(frozen.get("surface_scope_key") or ""),
        "topic_ratio_scope_key": str(frozen.get("topic_ratio_scope_key") or ""),
        "content_task_day": str(frozen.get("task_day") or ""),
        "route_family": str(frozen.get("route_family") or ""),
    }
    payload.update(_slot_vocabulary_payload(frozen))
    return payload


def _slot_vocabulary_payload(frozen: dict) -> dict[str, Any]:
    return {
        "daily_vocabulary_theme_id": int(
            frozen.get("daily_vocabulary_theme_id")
            if frozen.get("daily_vocabulary_theme_id") is not None
            else -1
        ),
        "daily_vocabulary_theme_version": str(
            frozen.get("daily_vocabulary_theme_version") or ""
        ),
        "daily_vocabulary_theme_effective_state": str(
            frozen.get("daily_vocabulary_theme_effective_state") or ""
        ),
        "vocabulary_catalog_version": str(frozen.get("vocabulary_catalog_version") or ""),
        "vocabulary_sample_ids": list(frozen.get("vocabulary_sample_ids") or []),
        "vocabulary_surface_terms": list(frozen.get("vocabulary_surface_terms") or []),
        "vocabulary_normalized_term_ids": list(
            frozen.get("vocabulary_normalized_term_ids") or []
        ),
        "vocabulary_candidate_count": int(frozen.get("vocabulary_candidate_count") or 0),
        "vocabulary_reservation_id": str(frozen.get("vocabulary_reservation_id") or ""),
    }


def _apply_mask_content_contract(payload: dict[str, Any], slot: SlotBuildInput) -> None:
    mask_id = str(payload.get("account_mask_id") or "")
    mask_version = int(payload.get("account_mask_version") or 0)
    snapshot_hash = str(payload.get("account_mask_snapshot_hash") or "")
    coverage_id = str(payload.get("coverage_ledger_id") or "")
    payload["daily_group_target_id"] = slot.blueprint.facts.coverage.daily_group_target_id
    if mask_id and mask_version > 0 and snapshot_hash:
        payload["content_source"] = "account_mask"
        payload["mask_status"] = "active"
        return
    payload["mask_status"] = "missing"
    if not coverage_id:
        payload["content_source"] = ""
        return
    payload["content_source"] = MASK_MISSING_CHECK_IN_SOURCE
    payload["act_type"] = "check_in"
    coverage = slot.blueprint.profile.coverage_rows.get(slot.account.id)
    target_date = coverage.coverage_date.isoformat() if coverage else _now().date().isoformat()
    payload["fallback_obligation_key"] = (
        f"{slot.blueprint.facts.task_id}:"
        f"{slot.blueprint.facts.group.id}:{slot.account.id}:"
        f"{target_date}:{MASK_MISSING_CHECK_IN_SOURCE}"
    )


def _slot_conversation_payload(slot: SlotBuildInput) -> dict[str, Any]:
    blueprint = slot.blueprint
    generation = blueprint.generation
    item = slot.item
    message_ids = generation.context_message_ids
    return {
        "topic_thread": blueprint.profile.topic_thread,
        "topic_plan": blueprint.profile.topic_plan,
        "intent": _intent_for_turn(slot.index),
        "chat_mode": generation.chat_mode,
        "anchor_message_ids": message_ids,
        "semantic_cluster": str(item.get("semantic_cluster") or ""),
        "duplicate_risk": str(item.get("duplicate_risk") or ""),
        "hallucination_risk": str(item.get("hallucination_risk") or ""),
        "quality_skip_reason": str(item.get("quality_skip_reason") or ""),
        "context_message_ids": message_ids,
        "context_snapshot_message_id": max(message_ids) if message_ids else None,
        "context_expire_after_messages": _context_expire_after_messages(blueprint.facts.config),
        "proactive_quiet_until_at": item.get("proactive_quiet_until_at"),
    }


def _context_expire_after_messages(config: dict[str, Any]) -> int:
    if "context_expire_after_messages" not in config:
        return 10
    return max(0, int(config.get("context_expire_after_messages") or 0))


def _slot_generation_payload(slot: SlotBuildInput) -> dict[str, Any]:
    blueprint = slot.blueprint
    profile = blueprint.profile
    generation = blueprint.generation
    preview = profile.profile_preview
    return {
        "ai_generation_id": profile.cycle_id,
        "ai_generation_status": "pending",
        "generation_source": generation.generation_source,
        **_provider_generation_payload(slot.item),
        "ai_generation_history": _deferred_ai_history(blueprint.turn.history),
        "ai_generation_tokens": 0,
        "ai_generation_count": len(generation.quality_items),
        "ai_generation_context_count": len(generation.context_message_ids),
        "ai_generation_memory_count": len(profile.account_memories),
        "profile_scene": str(preview.get("profile_scene") or GROUP_CHAT_SCENE),
        "profile_version": int(preview.get("profile_version") or 0),
        "profile_match_score": _profile_match_score(preview),
        "profile_match_reason": _profile_match_reason(preview),
        "profile_hit_summary": str(preview.get("profile_hit_summary") or ""),
        "profile_unavailable_reason": str(preview.get("profile_unavailable_reason") or ""),
    }


def _slot_rule_payload(slot: SlotBuildInput) -> dict[str, Any]:
    facts = slot.blueprint.facts
    item = slot.item
    rule_version = facts.rule_version
    fixed_version = bool(facts.config.get("rule_set_version_id"))
    return {
        "rule_set_id": rule_version.rule_set_id,
        "rule_set_name": facts.rule_set.name if facts.rule_set else "",
        "rule_set_version_id": rule_version.id,
        "resolved_rule_set_version_id": rule_version.id,
        "rule_set_version": rule_version.version,
        "rule_binding_mode": "fixed_version" if fixed_version else "follow_current",
        "reply_to_message_id": _reply_target_message_id(item),
        "reply_target_label": _reply_target_label(item),
        "reply_target_author": _reply_target_text(item, "author"),
        "reply_target_preview": _reply_target_text(item, "preview"),
        "reply_target_source": _reply_target_text(item, "source"),
        **_reply_target_binding_payload(item),
        "rule_trace": {
            "material_policy": (rule_version.routing or {}).get("material_policy"),
            "material_action": "",
            "material_intent": _material_intent_for_turn(item),
            "material_matched_tags": [],
            "material_candidate_count": 0,
            "material_id": None,
            "material_failure_reason": "",
        },
    }


def _freeze_content_mix_cycle(
    session: Session,
    task: Task,
    blueprint: PlanBlueprint,
) -> FrozenContentMix:
    target = _locked_content_mix_daily_target(
        session,
        blueprint.facts.coverage.daily_group_target_id,
    )
    if target is None or not target.task_day_ledger_id:
        raise ValueError("task_day_ledger_missing")
    items = blueprint.generation.quality_items
    alignment = _quantity_slot_alignment_for_content_mix(
        session,
        task,
        blueprint,
        target.task_day_ledger_id,
    )
    if alignment.code != "aligned":
        raise QuantitySlotAlignmentError(alignment)
    quantity_slots = list(alignment.slots)
    cycle = _existing_or_new_content_mix_cycle(
        session,
        task,
        blueprint,
        target.task_day_ledger_id,
        quantity_slots,
    )
    persisted = _content_mix_cycle_slots(session, cycle.id)
    logical_ids = [_quality_slot_id(item) for item in items]
    _clear_quantity_slot_alignment(task)
    return FrozenContentMix(
        cycle,
        dict(zip(logical_ids, persisted, strict=True)),
    )


def _locked_content_mix_daily_target(
    session: Session,
    target_id: str,
) -> TaskGroupDailyTarget | None:
    statement = select(TaskGroupDailyTarget).where(
        TaskGroupDailyTarget.id == target_id,
    )
    if session.bind and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update()
    return session.scalar(statement.execution_options(populate_existing=True))


def _quantity_slot_alignment_for_content_mix(
    session: Session,
    task: Task,
    blueprint: PlanBlueprint,
    ledger_id: str,
) -> QuantitySlotAlignmentResult:
    bound_slot = select(ContentMixCycleSlot.id).where(
        ContentMixCycleSlot.primary_quantity_slot_id
        == TaskGroupDailyMessageSlot.id,
    ).exists()
    statement = (
        select(TaskGroupDailyMessageSlot).where(
            TaskGroupDailyMessageSlot.task_id == task.id,
            TaskGroupDailyMessageSlot.task_day_ledger_id == ledger_id,
            TaskGroupDailyMessageSlot.state == "open",
            ~bound_slot,
        ).order_by(TaskGroupDailyMessageSlot.slot_ordinal.asc())
    )
    if session.bind and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update(of=TaskGroupDailyMessageSlot)
    available = list(session.scalars(statement))
    result = _quantity_slot_alignment(blueprint, available, ledger_id)
    if result.code == "aligned":
        return result
    return _classified_quantity_slot_alignment(
        session,
        task,
        ledger_id,
        result,
    )


def _align_quantity_slots(
    blueprint: PlanBlueprint,
    available: list[TaskGroupDailyMessageSlot],
) -> list[TaskGroupDailyMessageSlot]:
    return list(_quantity_slot_alignment(blueprint, available, "").slots)


def _quantity_slot_alignment(
    blueprint: PlanBlueprint,
    available: list[TaskGroupDailyMessageSlot],
    ledger_id: str,
) -> QuantitySlotAlignmentResult:
    remaining = list(available)
    selected: list[TaskGroupDailyMessageSlot] = []
    assigned_coverage_ids: set[str] = set()
    missing_coverage_ids: list[str] = []
    missing_extra_count = 0
    for item in blueprint.generation.quality_items:
        account_id = _quality_slot_account_id(item)
        claim_id = _reply_target_text(item, "conversation_turn_claim_id")
        coverage = blueprint.profile.coverage_rows.get(account_id)
        coverage_id = _incomplete_coverage_id(coverage)
        expected_coverage_id = (
            None
            if not coverage_id or coverage_id in assigned_coverage_ids
            else coverage_id
        )
        matched = _matching_quantity_slot(
            remaining,
            claim_id=claim_id,
            expected_coverage_id=expected_coverage_id,
        )
        if matched is None:
            if expected_coverage_id:
                missing_coverage_ids.append(expected_coverage_id)
            else:
                missing_extra_count += 1
            continue
        selected.append(matched)
        remaining.remove(matched)
        if expected_coverage_id:
            assigned_coverage_ids.add(expected_coverage_id)
    requested_count = len(blueprint.generation.quality_items)
    code = "aligned" if len(selected) == requested_count else "unclassified"
    return QuantitySlotAlignmentResult(
        code=code,
        ledger_id=ledger_id,
        slots=tuple(selected),
        requested_count=requested_count,
        missing_coverage_ids=tuple(missing_coverage_ids),
        missing_extra_count=missing_extra_count,
    )


def _matching_quantity_slot(
    available: list[TaskGroupDailyMessageSlot],
    *,
    claim_id: str,
    expected_coverage_id: str | None,
) -> TaskGroupDailyMessageSlot | None:
    for slot in available:
        if claim_id and str(slot.continuity_claim_id or "") == claim_id:
            return slot
        if not claim_id and (
            slot.continuity_claim_id is None
            and slot.task_account_daily_coverage_id == expected_coverage_id
        ):
            return slot
    return None


def _classified_quantity_slot_alignment(
    session: Session,
    task: Task,
    ledger_id: str,
    result: QuantitySlotAlignmentResult,
) -> QuantitySlotAlignmentResult:
    code = _quantity_slot_alignment_failure_code(
        session,
        task,
        ledger_id,
        result,
    )
    return QuantitySlotAlignmentResult(
        code=code,
        ledger_id=ledger_id,
        slots=result.slots,
        requested_count=result.requested_count,
        missing_coverage_ids=result.missing_coverage_ids,
        missing_extra_count=result.missing_extra_count,
    )


def _quantity_slot_alignment_failure_code(
    session: Session,
    task: Task,
    ledger_id: str,
    result: QuantitySlotAlignmentResult,
) -> str:
    if not result.missing_coverage_ids:
        return (
            "extra_volume_slot_unavailable"
            if result.missing_extra_count
            else "quantity_slot_state_changed"
        )
    rows = list(session.scalars(select(TaskGroupDailyMessageSlot).where(
        TaskGroupDailyMessageSlot.task_id == task.id,
        TaskGroupDailyMessageSlot.task_day_ledger_id == ledger_id,
        TaskGroupDailyMessageSlot.task_account_daily_coverage_id.in_(
            result.missing_coverage_ids,
        ),
    )))
    found_ids = {str(row.task_account_daily_coverage_id) for row in rows}
    if set(result.missing_coverage_ids) - found_ids:
        return "quantity_slot_invariant_mismatch"
    if _quantity_slots_are_content_mix_bound(session, rows):
        return "existing_cycle_replan_required"
    return "quantity_slot_state_changed"


def _quantity_slots_are_content_mix_bound(
    session: Session,
    rows: list[TaskGroupDailyMessageSlot],
) -> bool:
    slot_ids = [row.id for row in rows]
    if not slot_ids:
        return False
    return bool(session.scalar(select(ContentMixCycleSlot.id).where(
        ContentMixCycleSlot.primary_quantity_slot_id.in_(slot_ids),
    ).limit(1)))


def _incomplete_coverage_id(
    coverage: TaskAccountDailyCoverage | None,
) -> str:
    if coverage is None:
        return ""
    target = max(1, int(getattr(coverage, "target_count", 1) or 1))
    if int(getattr(coverage, "confirmed_count", 0) or 0) >= target:
        return ""
    return str(coverage.id)


def _existing_or_new_content_mix_cycle(
    session: Session,
    task: Task,
    blueprint: PlanBlueprint,
    ledger_id: str,
    quantity_slots: list[TaskGroupDailyMessageSlot],
) -> ContentMixCycle:
    target_id = _content_mix_target_id(blueprint, quantity_slots)
    existing = session.scalar(select(ContentMixCycle).where(
        ContentMixCycle.task_id == task.id,
        ContentMixCycle.target_operation_target_id == target_id,
        ContentMixCycle.task_day_ledger_id == ledger_id,
        ContentMixCycle.cycle_seq == blueprint.turn.cycle_index,
    ))
    if existing:
        return existing
    return create_content_mix_cycle(
        session,
        _content_mix_spec(task, blueprint, ledger_id, target_id, quantity_slots),
    )


def _content_mix_spec(
    task: Task,
    blueprint: PlanBlueprint,
    ledger_id: str,
    target_id: int,
    quantity_slots: list[TaskGroupDailyMessageSlot],
) -> ContentMixCycleSpec:
    items = blueprint.generation.quality_items
    slots = tuple(
        ContentMixSlotSpec(
            primary_quantity_slot_id=quantity.id,
            relation_kind="reply" if _reply_target_message_id(item) else "direct",
            reply_requirement_key=_quality_slot_id(item) if _reply_target_message_id(item) else "",
            initial_reply_to_message_id=str(_reply_target_message_id(item) or ""),
        )
        for item, quantity in zip(items, quantity_slots, strict=True)
    )
    return ContentMixCycleSpec(
        tenant_id=task.tenant_id,
        task_id=task.id,
        target_operation_target_id=target_id,
        task_day_ledger_id=ledger_id,
        cycle_seq=blueprint.turn.cycle_index,
        config_revision=blueprint.facts.task_config_revision,
        allocation_seed=blueprint.profile.cycle_id,
        slots=slots,
        reply_min_required_count=(
            0
            if blueprint.generation.coverage_reply_shortfall
            else min(len(slots), blueprint.generation.requested_reply_count)
        ),
        material_policy_rule_set_id=str(blueprint.facts.rule_version.rule_set_id),
        material_policy_rule_set_version=int(blueprint.facts.rule_version.version),
        target_resolution_trace=json.dumps(
            {
                "operation_target_id": target_id,
                "group_id": blueprint.facts.group.id,
                "group_peer_id": blueprint.facts.group.tg_peer_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


def _content_mix_target_id(
    blueprint: PlanBlueprint,
    quantity_slots: list[TaskGroupDailyMessageSlot],
) -> int:
    target = blueprint.facts.target
    target_id = int(
        target.id if target
        else blueprint.facts.config.get("target_operation_target_id") or 0
    )
    if target_id <= 0 and quantity_slots:
        target_id = int(quantity_slots[0].target_operation_target_id)
    if target_id <= 0:
        raise ValueError("content_mix_target_missing")
    return target_id


def _content_mix_cycle_slots(
    session: Session,
    cycle_id: str,
) -> list[ContentMixCycleSlot]:
    return list(session.scalars(
        select(ContentMixCycleSlot)
        .where(ContentMixCycleSlot.cycle_id == cycle_id)
        .order_by(ContentMixCycleSlot.slot_index.asc())
    ))


def _prepare_action_slots(
    session: Session,
    task: Task,
    blueprint: PlanBlueprint,
    frozen_mix: FrozenContentMix | None,
) -> PreparedActionPlan:
    generation = blueprint.generation
    progress = blueprint.facts.hard_progress
    used_ids: set[int] = set()
    blockers: dict[str, int] = {}
    slots: list[SlotSnapshot] = []
    reservations: list[AccountCapacityReservation] = []
    capacity_cache = AccountCapacityCache()
    burst_account = None
    for index, item in enumerate(generation.quality_items):
        account, planned_at = _choose_action_slot_account(
            session,
            task,
            blueprint,
            item=item,
            index=index,
            burst_account=burst_account,
            used_account_ids=used_ids,
            reservations=reservations,
            capacity_cache=capacity_cache,
        )
        if not account:
            _record_action_slot_blocker(task, blockers, progress, kind="account_capacity")
            continue
        prepared = _paced_slot_snapshot(
            session,
            task,
            blueprint,
            item=item,
            index=index,
            account=account,
            planned_at=planned_at,
            frozen_mix=frozen_mix,
        )
        if prepared is None:
            _record_action_slot_blocker(task, blockers, progress, kind="account_timeline")
            continue
        snapshot, planned_at = prepared
        if generation.burst_plan and index == min(generation.burst_plan):
            burst_account = account
        _record_prepared_action_slot(
            blueprint, account, snapshot, planned_at=planned_at,
            used_ids=used_ids, slots=slots, reservations=reservations,
        )
    return PreparedActionPlan(slots, blockers)


def _record_action_slot_blocker(
    task: Task,
    blockers: dict[str, int],
    progress: dict[str, Any],
    *,
    kind: str,
) -> None:
    _hard_blocker_inc(blockers, kind, progress)
    stats_inc(task, "skipped_count")


def _record_prepared_action_slot(
    blueprint: PlanBlueprint,
    account,
    snapshot: SlotSnapshot,
    *,
    planned_at: datetime,
    used_ids: set[int],
    slots: list[SlotSnapshot],
    reservations: list[AccountCapacityReservation],
) -> None:
    used_ids.add(account.id)
    slots.append(snapshot)
    _increment_coverage_count(
        blueprint.turn.round_config,
        account.id,
        blueprint.profile.coverage_counts,
    )
    reservations.append(
        AccountCapacityReservation(account_id=account.id, scheduled_at=planned_at),
    )


def _choose_action_slot_account(
    session: Session,
    task: Task,
    blueprint: PlanBlueprint,
    *,
    item: dict,
    index: int,
    burst_account,
    used_account_ids: set[int],
    reservations: list[AccountCapacityReservation],
    capacity_cache: AccountCapacityCache,
):
    candidates = _slot_candidate_accounts(
        blueprint, item, index, burst_account=burst_account,
    )
    return _choose_capacity_slot(
        session,
        task,
        selected=candidates,
        planned_at=blueprint.generation.times[index],
        index=index,
        used_account_ids=used_account_ids,
        allow_repeat=bool(blueprint.turn.round_config.get("allow_account_repeat", True)),
        progress=blueprint.facts.hard_progress,
        reservations=reservations,
        capacity_cache=capacity_cache,
    )


def _paced_slot_snapshot(
    session: Session,
    task: Task,
    blueprint: PlanBlueprint,
    *,
    item: dict,
    index: int,
    account,
    planned_at: datetime,
    frozen_mix: FrozenContentMix | None,
) -> tuple[SlotSnapshot, datetime] | None:
    pacing = _reserve_ai_action_pacing(
        session, task, account_id=account.id, item=item, planned_at=planned_at,
    )
    if pacing is None:
        return None
    planned_at, pacing_owner, pacing_slot_key, pacing_reservation = pacing
    snapshot = _build_slot_snapshot(
        SlotBuildInput(blueprint, account, index, item, planned_at),
    )
    if frozen_mix is not None:
        snapshot = _with_frozen_content_mix(snapshot, item, frozen_mix)
    snapshot = _with_content_variation_key(snapshot)
    return replace(
        snapshot,
        pacing_owner=pacing_owner,
        pacing_slot_key=pacing_slot_key,
        pacing_reservation=pacing_reservation,
    ), planned_at


def _reserve_ai_action_pacing(
    session: Session,
    task: Task,
    *,
    account_id: int,
    item: dict,
    planned_at: datetime,
) -> tuple[datetime, TaskGroupDailyMessageSlot | None, str, Any | None] | None:
    if task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION:
        return planned_at, None, "", None
    owner_id = str(item.get("pacing_quantity_slot_id") or "")
    owner = session.get(TaskGroupDailyMessageSlot, owner_id)
    if owner is None or owner.pacing_due_at is None:
        raise ValueError("ai_pacing_owner_missing")
    slot_key = str(item.get("pacing_slot_key") or f"ai:{owner.id}")
    deadline_at = item.get("pacing_deadline_at")
    try:
        reservation = reserve_account_pacing(
            session,
            tenant_id=task.tenant_id,
            task_id=task.id,
            account_id=account_id,
            slot_key=slot_key,
            due_at=owner.pacing_due_at,
            release_not_before_at=latest_wall_datetime(
                owner.release_not_before_at or owner.pacing_due_at,
                planned_at,
            ),
            deadline_at=deadline_at,
            engagement_contract_version=str(
                (task.type_config or {}).get("engagement_contract_version") or ""
            ),
            action_class="authored_message",
            allow_session_wake=bool(
                _reply_target_text(item, "conversation_turn_claim_id")
            ),
        )
    except AccountPacingDeadlineExceeded:
        _record_ai_pacing_shortfall(task, 1, 0)
        return None
    except AccountPacingLockUnavailable:
        return None
    return reservation.effective_claim_at, owner, slot_key, reservation


def _with_frozen_content_mix(
    snapshot: SlotSnapshot,
    item: dict,
    frozen: FrozenContentMix,
) -> SlotSnapshot:
    logical_id = _quality_slot_id(item)
    cycle_slot = frozen.slots_by_logical_id[logical_id]
    payload = snapshot.payload.model_copy(update={
        "content_mix_cycle_id": frozen.cycle.id,
        "content_mix_cycle_slot_id": cycle_slot.id,
        "primary_quantity_slot_id": cycle_slot.primary_quantity_slot_id,
        "content_mix_contract_version": frozen.cycle.config_revision,
        "relation_kind": cycle_slot.relation_kind,
        "slot_attempt": max(1, cycle_slot.slot_attempt or 1),
    })
    return replace(snapshot, payload=payload)


def _slot_candidate_accounts(
    blueprint: PlanBlueprint, item: dict, index: int, *, burst_account: Any,
) -> list[Any]:
    if burst_account and index in blueprint.generation.burst_plan:
        return [burst_account]
    slot_id = _quality_slot_account_id(item)
    account_by_id = {account.id: account for account in blueprint.profile.selected}
    slot_account = account_by_id.get(slot_id)
    return [slot_account] if slot_account else blueprint.profile.selected


def _prepared_plan_is_blocked(
    task: Task,
    blueprint: PlanBlueprint,
    prepared: PreparedActionPlan,
    *,
    prepared_reply_count: int,
) -> bool:
    if prepared_reply_count < blueprint.generation.requested_reply_count:
        stats_inc(task, "reply_candidate_shortfall_count")
        if not blueprint.facts.hard_progress:
            task.last_error = "AI 引用回复候选不足，已跳过本轮"
            return True
    account_ids = [slot.account_id for slot in prepared.slots]
    skew = _hard_hourly_distribution_skew(account_ids, len(blueprint.profile.selected))
    if not blueprint.facts.hard_progress or not skew:
        return False
    _record_hard_hourly_distribution_skew(task, skew)
    deficit = max(
        1, int(blueprint.facts.hard_progress.get("deficit") or len(prepared.slots)),
    )
    mark_plan_result(
        task, blueprint.facts.hard_progress, 0, {"account_distribution_skew": deficit},
    )
    return True


def _create_reserved_actions(
    session: Session,
    task: Task,
    *,
    blueprint: PlanBlueprint,
    prepared: PreparedActionPlan,
) -> int:
    created = 0
    created_actions: list[Action] = []
    reserved_rows: list[TaskAccountDailyCoverage] = []
    for slot in prepared.slots:
        action = _create_reserved_action(session, task, slot)
        if action is None:
            continue
        created += 1
        created_actions.append(action)
        _remember_reserved_coverage_row(
            reserved_rows,
            blueprint.profile.coverage_rows,
            account_id=slot.account_id,
            payload=slot.payload,
        )
    _advance_reserved_coverage_cursor(session, task, reserved_rows)
    if dict(task.type_config or {}).get("ai_dialogue_chain_enabled"):
        from ..ai_dialogue_chain import link_existing_dialogue_chain

        link_existing_dialogue_chain(
            task,
            created_actions,
            context_mode=_context_mode(
                blueprint.context.usable_rows,
                blueprint.context.idle_continuation,
            ),
        )
    return created


def _create_reserved_action(session: Session, task: Task, slot: SlotSnapshot) -> Action | None:
    payload = _with_group_bot_admission_snapshot(
        session,
        task,
        slot.account_id,
        slot.payload,
    )
    coverage_id = str(payload.coverage_ledger_id or "")
    if not coverage_id:
        action = create_send_action(
            session,
            task,
            slot.account_id,
            slot.planned_at,
            payload,
        )
        _bind_ai_action_pacing(action, slot)
        _bind_turn_claim(session, action)
        if task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION:
            _bind_content_mix_action(session, action, payload)
        return action
    reservation_token = str(uuid4())
    if not _reserve_coverage_before_action(
        session,
        coverage_id,
        reservation_token,
        allow_pending_admission=(
            task.fulfillment_contract_version == "fact_first_v3"
        ),
    ):
        return None
    intent = _create_coverage_variation_intent(session, task, payload)
    if intent is None:
        _release_variation_conflict(session, coverage_id, reservation_token)
        return None
    action_id = str(uuid4())
    action = create_send_action(session, task, slot.account_id, slot.planned_at, payload, action_id=action_id)
    if action.id != action_id:
        intent.outcome = "action_deduplicated"
        _release_variation_conflict(session, coverage_id, reservation_token)
        return None
    _bind_ai_action_pacing(action, slot)
    _bind_turn_claim(session, action)
    intent.action_id = action.id
    if not bind_coverage_reservation(session, coverage_id, reservation_token, action.id):
        raise RuntimeError("daily coverage reservation lost before action binding")
    if task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION:
        _bind_content_mix_action(session, action, payload)
    session.flush()
    return action


def _bind_turn_claim(session: Session, action: Action) -> None:
    from ..engagement_conversation import bind_conversation_turn_claim

    bind_conversation_turn_claim(session, action)


def _bind_ai_action_pacing(action: Action, slot: SlotSnapshot) -> None:
    if slot.pacing_owner is None:
        return
    if slot.pacing_reservation is None or not slot.pacing_slot_key:
        raise ValueError("ai_pacing_reservation_missing")
    action.primary_quantity_slot_id = slot.pacing_owner.id
    freeze_action_pacing(
        action,
        slot.pacing_owner,
        slot_key=slot.pacing_slot_key,
    )
    bind_account_pacing_reservation(slot.pacing_reservation, action)


def _with_group_bot_admission_snapshot(
    session: Session,
    task: Task,
    account_id: int,
    payload: SendMessagePayload,
) -> SendMessagePayload:
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        return payload
    config = task.type_config if isinstance(task.type_config, dict) else {}
    if _group_bot_admission_requirement(config) is False:
        return payload
    admission = session.scalar(select(GroupBotAdmission).where(
        GroupBotAdmission.tenant_id == task.tenant_id,
        GroupBotAdmission.group_id == payload.group_id,
        GroupBotAdmission.account_id == account_id,
    ))
    if admission is None:
        return payload
    return payload.model_copy(update={
        "group_bot_admission_id": admission.id,
        "group_bot_admission_state": admission.state,
        "admission_version": int(admission.admission_version or 1),
    })


def _bind_content_mix_action(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
) -> None:
    cycle_slot = session.get(ContentMixCycleSlot, payload.content_mix_cycle_slot_id)
    if cycle_slot is None:
        raise RuntimeError("content_mix_cycle_slot_not_found")
    action.primary_quantity_slot_id = payload.primary_quantity_slot_id
    action.content_mix_cycle_slot_id = cycle_slot.id
    action.content_mix_slot_attempt = max(1, payload.slot_attempt)
    mark_cycle_slot_materialized(
        session,
        cycle_slot,
        action_id=action.id,
        slot_attempt=action.content_mix_slot_attempt,
    )


def _reserve_coverage_before_action(
    session: Session,
    coverage_id: str,
    reservation_token: str,
    *,
    allow_pending_admission: bool = False,
) -> bool:
    allowed_states = (
        ("ready", "pending_admission")
        if allow_pending_admission
        else ("ready",)
    )
    return reserve_coverage_for_planned_action(
        session,
        coverage_id,
        reservation_token,
        now=_now(),
        allowed_states=allowed_states,
    )


def _create_coverage_variation_intent(
    session: Session,
    task: Task,
    payload: SendMessagePayload,
) -> AiCoverageVariationIntent | None:
    variation_key = str(payload.content_variation_key or "")
    coverage_id = str(payload.coverage_ledger_id or "")
    if not variation_key:
        raise ValueError("daily coverage action requires content_variation_key")
    snapshot = _variation_intent_snapshot(payload)
    intent = AiCoverageVariationIntent(
        tenant_id=task.tenant_id,
        coverage_ledger_id=coverage_id,
        action_id=None,
        content_variation_key=variation_key,
        context_version=str(payload.content_context_version or ""),
        intent_snapshot_hash=hashlib.sha256(snapshot).hexdigest(),
        outcome="reserved",
    )
    try:
        with session.begin_nested():
            session.add(intent)
            session.flush()
    except IntegrityError:
        return None
    return intent


def _variation_intent_snapshot(payload: SendMessagePayload) -> bytes:
    source = {
        "variation_key": payload.content_variation_key,
        "context_version": payload.content_context_version,
        "target_reference_revision": payload.target_reference_revision,
        "coverage_window_date": payload.coverage_window_date,
        "act_type": payload.act_type,
        "reply_to_message_id": payload.reply_to_message_id,
    }
    return json.dumps(source, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")


def _release_variation_conflict(session: Session, coverage_id: str, reservation_token: str) -> None:
    release_planned_coverage_reservation(
        session,
        coverage_id,
        reservation_token,
        blocker_code="content_variation_key_conflict",
        blocker_detail="当前覆盖义务已使用相同内容变体，等待新的上下文版本",
    )


def _record_plan_completion(
    session: Session,
    task: Task,
    blueprint: PlanBlueprint,
    *,
    prepared: PreparedActionPlan,
    prepared_reply_count: int,
    created: int,
) -> None:
    context = blueprint.context
    for row in context.unprocessed_rows:
        remember_fingerprint(
            session, task.tenant_id, context.fingerprint_source, _context_fingerprint(row),
        )
    stats = dict(task.stats or {})
    stats.update(
        {
            "current_mode": context.mode,
            "ramp_ratio": context.ramp_ratio,
            "context_mode": _context_mode(context.usable_rows, context.idle_continuation),
            "chat_mode": blueprint.generation.chat_mode,
            "generation_source": blueprint.generation.generation_source,
            "reply_planned_count": prepared_reply_count,
        },
    )
    for key in (
        "duplicate_risk", "hallucination_risk", "skip_reason",
        "idle_continuation_next_run_at", "force_bootstrap_once",
    ):
        stats.pop(key, None)
    progress = blueprint.facts.hard_progress
    task.last_error = _hard_blocked_last_error(
        task,
        created=created,
        blockers=prepared.hard_blockers,
        progress=progress,
    )
    task.stats = stats
    if progress:
        mark_plan_result(task, progress, created, prepared.hard_blockers or None)
    stats_inc(task, "total_rounds")


def build_plan(session: Session, task: Task) -> int:
    from ..admission_epoch_recovery import replan_stale_admission_actions

    replan_stale_admission_actions(session, task=task)
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        _recover_stale_fact_first_actions(session, task)
    else:
        recover_stale_pending_content_mix_slots(session, task)
    blueprint = _prepare_plan_blueprint(session, task)
    if isinstance(blueprint, PlanAbort):
        return blueprint.created
    frozen_mix = None
    if task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION:
        replan = _replan_content_mix_slots(session, task, blueprint)
        if replan.created > 0:
            return replan.created
        if replan.found:
            blueprint = _prepare_plan_blueprint(
                session,
                task,
                include_replan_accounts=False,
            )
            if isinstance(blueprint, PlanAbort):
                return blueprint.created
        try:
            frozen_mix = _freeze_content_mix_cycle(session, task, blueprint)
        except QuantitySlotAlignmentError as exc:
            _record_quantity_slot_alignment_failure(task, exc.result)
            return 0
    prepared = _prepare_action_slots(session, task, blueprint, frozen_mix)
    prepared_reply_count = sum(
        1 for slot in prepared.slots if slot.payload.reply_to_message_id
    )
    if _prepared_plan_is_blocked(
        task, blueprint, prepared, prepared_reply_count=prepared_reply_count,
    ):
        return 0
    created = _create_reserved_actions(
        session, task, blueprint=blueprint, prepared=prepared,
    )
    _record_plan_completion(
        session,
        task,
        blueprint,
        prepared=prepared,
        prepared_reply_count=prepared_reply_count,
        created=created,
    )
    return created


def _recover_stale_fact_first_actions(session: Session, task: Task) -> int:
    actions = _recoverable_fact_first_actions(session, task)
    recovered = 0
    for action in actions:
        payload = action.payload if isinstance(action.payload, dict) else {}
        coverage_id = str(payload.get("coverage_ledger_id") or "")
        if not coverage_id:
            continue
        result = dict(action.result or {})
        released = release_coverage_reservation(
            session,
            coverage_id,
            action.id,
            blocker_code=str(result.get("error_code") or action.status),
            blocker_detail=str(result.get("error_message") or ""),
        )
        _cancel_fact_first_variation_intent(session, action.id)
        recovered += int(released)
    return recovered


def _recoverable_fact_first_actions(session: Session, task: Task):
    gateway_started = select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == Action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).exists()
    candidate_ids = _fact_first_recovery_action_ids(task).subquery()
    return session.scalars(
        select(Action).join(
            candidate_ids,
            candidate_ids.c.action_id == Action.id,
        ).where(
            Action.task_id == task.id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status.in_(FACT_FIRST_REBUILD_ACTION_STATUSES),
            ~gateway_started,
        )
        .order_by(Action.created_at, Action.id)
        .limit(FACT_FIRST_STALE_RECOVERY_BATCH_LIMIT)
    )


def _fact_first_recovery_action_ids(task: Task):
    coverage_actions = select(
        TaskAccountDailyCoverage.reserved_action_id.label("action_id"),
    ).where(
        TaskAccountDailyCoverage.task_id == task.id,
        TaskAccountDailyCoverage.reserved_action_id.is_not(None),
    )
    intent_actions = (
        select(AiCoverageVariationIntent.action_id.label("action_id"))
        .join(
            TaskAccountDailyCoverage,
            TaskAccountDailyCoverage.id
            == AiCoverageVariationIntent.coverage_ledger_id,
        )
        .where(
            TaskAccountDailyCoverage.task_id == task.id,
            AiCoverageVariationIntent.action_id.is_not(None),
        )
    )
    return union(coverage_actions, intent_actions)


def _cancel_fact_first_variation_intent(session: Session, action_id: str) -> None:
    session.execute(
        update(AiCoverageVariationIntent)
        .where(AiCoverageVariationIntent.action_id == action_id)
        .values(
            action_id=None,
            outcome="cancelled_pre_gateway_rebuild",
            updated_at=_now(),
        )
    )


def _record_quantity_slot_alignment_failure(
    task: Task,
    result: QuantitySlotAlignmentResult,
) -> None:
    messages = {
        "existing_cycle_replan_required": "数量槽已绑定旧内容周期，等待原槽重建",
        "quantity_slot_state_changed": "数量槽状态已变化，等待重新规划",
        "extra_volume_slot_unavailable": "当前没有可用的额外消息数量槽",
        "quantity_slot_invariant_mismatch": "数量槽与账号覆盖身份不一致，需要修复账本",
    }
    stats = dict(task.stats or {})
    stats["quantity_slot_alignment"] = {
        "code": result.code,
        "ledger_id": result.ledger_id,
        "requested_count": result.requested_count,
        "aligned_count": result.aligned_count,
        "missing_coverage_ids": list(result.missing_coverage_ids[:20]),
        "missing_extra_count": result.missing_extra_count,
        "recorded_at": _now().isoformat(),
    }
    task.stats = stats
    task.last_error = messages[result.code]


def _clear_quantity_slot_alignment(task: Task) -> None:
    stats = dict(task.stats or {})
    if "quantity_slot_alignment" not in stats:
        return
    stats.pop("quantity_slot_alignment", None)
    task.stats = stats


def _replan_content_mix_slots(
    session: Session,
    task: Task,
    blueprint: PlanBlueprint,
) -> ContentMixReplanResult:
    ledger_id = _content_mix_replan_ledger_id(session, blueprint)
    account_by_id = {item.id: item for item in blueprint.profile.selected}
    rows = _content_mix_replan_rows(
        session,
        task,
        ledger_id,
        account_ids=set(account_by_id),
    )
    if not rows:
        return ContentMixReplanResult(False)
    created = 0
    for cycle_slot, previous, coverage in rows:
        resolved = _replan_slot_account_and_item(
            blueprint,
            account_by_id,
            cycle_slot,
            previous=previous,
            coverage=coverage,
        )
        if resolved is None:
            continue
        account, item_index = resolved
        snapshot = _replan_slot_snapshot(
            blueprint,
            account,
            item_index,
            cycle_slot,
            previous,
        )
        if _create_reserved_action(session, task, snapshot) is not None:
            created += 1
    if created == 0:
        task.last_error = "内容合同槽位等待可用账号或合法引用对象后重建"
        task.next_run_at = _now() + timedelta(seconds=DAILY_COVERAGE_DEBT_RECHECK_SECONDS)
    return ContentMixReplanResult(True, created)


def _content_mix_replan_rows(
    session: Session,
    task: Task,
    ledger_id: str,
    *,
    account_ids: set[int],
) -> list[tuple[ContentMixCycleSlot, Action | None, TaskAccountDailyCoverage | None]]:
    if not ledger_id or not account_ids:
        return []
    return list(session.execute(
        select(ContentMixCycleSlot, Action)
        .join(ContentMixCycle, ContentMixCycle.id == ContentMixCycleSlot.cycle_id)
        .join(
            TaskGroupDailyMessageSlot,
            TaskGroupDailyMessageSlot.id
            == ContentMixCycleSlot.primary_quantity_slot_id,
        )
        .outerjoin(Action, Action.id == ContentMixCycleSlot.current_action_id)
        .outerjoin(
            TaskAccountDailyCoverage,
            TaskAccountDailyCoverage.id
            == TaskGroupDailyMessageSlot.task_account_daily_coverage_id,
        )
        .add_columns(TaskAccountDailyCoverage)
        .where(
            ContentMixCycle.task_id == task.id,
            ContentMixCycle.task_day_ledger_id == ledger_id,
            ContentMixCycleSlot.slot_state.in_(
                {"unmaterialized", "replan_required"},
            ),
            func.coalesce(
                TaskAccountDailyCoverage.account_id,
                Action.account_id,
            ).in_(account_ids),
        )
        .order_by(ContentMixCycle.cycle_seq, ContentMixCycleSlot.slot_index)
        .limit(MAX_DAILY_COVERAGE_PLAN_BATCH)
        .with_for_update(of=ContentMixCycleSlot, skip_locked=True)
    ))


def _content_mix_replan_ledger_id(
    session: Session,
    blueprint: PlanBlueprint,
) -> str:
    target_id = str(blueprint.facts.coverage.daily_group_target_id or "")
    target = session.get(TaskGroupDailyTarget, target_id) if target_id else None
    if target is not None and target.task_day_ledger_id:
        return str(target.task_day_ledger_id)
    rows = blueprint.profile.coverage_rows.values()
    first = next(iter(rows), None)
    return str(first.task_day_ledger_id or "") if first is not None else ""


def _replan_slot_account_and_item(
    blueprint: PlanBlueprint,
    account_by_id: dict[int, Any],
    cycle_slot: ContentMixCycleSlot,
    *,
    previous: Action | None,
    coverage: TaskAccountDailyCoverage | None,
) -> tuple[Any, int] | None:
    account_id = int(
        coverage.account_id if coverage
        else previous.account_id if previous and previous.account_id
        else 0
    )
    item_index = _replan_generation_item_index(
        blueprint.generation.quality_items,
        relation_kind=cycle_slot.relation_kind,
        account_id=account_id,
    )
    if item_index is None:
        return None
    if not account_id:
        item = blueprint.generation.quality_items[item_index]
        account_id = _quality_slot_account_id(item)
    account = account_by_id.get(account_id)
    return (account, item_index) if account is not None else None


def _replan_generation_item_index(
    items: list[dict],
    *,
    relation_kind: str,
    account_id: int = 0,
) -> int | None:
    fallback: int | None = None
    for index, item in enumerate(items):
        has_reply = bool(_reply_target_message_id(item))
        if has_reply != (relation_kind == "reply"):
            continue
        if fallback is None:
            fallback = index
        if account_id and _quality_slot_account_id(item) == account_id:
            return index
    return fallback


def _replan_slot_snapshot(
    blueprint: PlanBlueprint,
    account: Any,
    item_index: int,
    cycle_slot: ContentMixCycleSlot,
    previous: Action | None,
) -> SlotSnapshot:
    planned_at = blueprint.generation.times[
        min(item_index, len(blueprint.generation.times) - 1)
    ]
    fresh = _build_slot_snapshot(
        SlotBuildInput(
            blueprint,
            account,
            item_index,
            blueprint.generation.quality_items[item_index],
            planned_at,
        )
    )
    payload = _replan_slot_payload(
        fresh.payload,
        previous.payload if previous is not None else {},
        cycle_slot,
    )
    return _with_content_variation_key(
        SlotSnapshot(account.id, planned_at, payload)
    )


def _replan_slot_payload(
    fresh: SendMessagePayload,
    previous_payload: dict,
    cycle_slot: ContentMixCycleSlot,
) -> SendMessagePayload:
    old = previous_payload if isinstance(previous_payload, dict) else {}
    payload_data = fresh.model_dump(mode="python")
    for field in CONTENT_MIX_REPLAN_PRESERVED_FIELDS:
        payload_data[field] = old.get(field, getattr(fresh, field))
    payload_data.update({
        "content_mix_cycle_id": cycle_slot.cycle_id,
        "content_mix_cycle_slot_id": cycle_slot.id,
        "primary_quantity_slot_id": cycle_slot.primary_quantity_slot_id,
        "relation_kind": cycle_slot.relation_kind,
        "slot_attempt": cycle_slot.slot_attempt + 1,
        "planned_material_kind": "unresolved",
        "planned_normal_text_emoji": "unresolved",
    })
    return SendMessagePayload.model_validate(payload_data)


def _remember_reserved_coverage_row(
    reserved_rows: list[TaskAccountDailyCoverage],
    rows_by_account: dict[int, TaskAccountDailyCoverage],
    *,
    account_id: int,
    payload: SendMessagePayload,
) -> None:
    if not payload.coverage_ledger_id:
        return
    row = rows_by_account.get(account_id)
    if row is not None:
        reserved_rows.append(row)


def _advance_reserved_coverage_cursor(
    session: Session,
    task: Task,
    reserved_rows: list[TaskAccountDailyCoverage],
) -> None:
    if not reserved_rows:
        return
    cursor_row = max(reserved_rows, key=lambda row: (row.targeted_at, row.account_id, row.id))
    advance_coverage_plan_cursor(session, task, cursor_row, now=_now())


def prepare_open_actions_for_planning(session: Session, task: Task) -> int:
    config = {**(task.type_config or {}), "pacing_config": task.pacing_config or {}}
    config = _canonicalized_task_config(session, task, config)
    legacy_replanned = expire_legacy_anchor_rewritten_actions(session, task)
    if _daily_coverage_enforced(config):
        legacy_replanned += expire_incomplete_daily_contract_actions(session, task)
    legacy_replanned += _skip_legacy_hard_hourly_open_actions_for_daily_coverage_replan(session, task, config)
    if (
        task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION
        and _all_accounts_daily_coverage(config)
    ):
        return legacy_replanned
    group = group_from_reference(
        session,
        task.tenant_id,
        group_id=int(config.get("target_group_id") or 0) or None,
        operation_target_id=int(config.get("target_operation_target_id") or 0) or None,
        require_authorized=False,
    )
    if not group:
        return legacy_replanned
    legacy_replanned += _backfill_open_action_admission_snapshots(
        session,
        task,
    )
    hard_progress = (
        current_progress(session, task, _now())
        if hard_hourly_enabled(task)
        and task.fulfillment_contract_version != "fact_first_v3"
        else {}
    )
    hard_progress = hard_progress if int(hard_progress.get("deficit") or 0) > 0 else {}
    accounts = _select_accounts_for_plan(session, task, group, hard_progress, config)
    accounts = _online_ready_accounts(session, task, accounts, hard_progress)
    if not accounts:
        return legacy_replanned
    skipped = _skip_skewed_hard_hourly_open_actions_for_replan(session, task, len(accounts))
    if _all_accounts_daily_coverage(config):
        return legacy_replanned + skipped
    ready_voice_profiles = voice_profile_prompt_details(
        session,
        tenant_id=task.tenant_id,
        account_ids=[account.id for account in accounts],
    )
    return legacy_replanned + skipped + _expire_open_profileless_actions(session, task, ready_voice_profiles.keys())


def _backfill_open_action_admission_snapshots(
    session: Session,
    task: Task,
) -> int:
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        return 0
    actions = list(session.scalars(select(Action).where(
        Action.task_id == task.id,
        Action.action_type == "send_message",
        Action.status.in_(("pending", "retryable_failed")),
        func.coalesce(
            Action.payload["group_bot_admission_state"].as_string(),
            "",
        ) == "",
    )))
    updated = 0
    for action in actions:
        if action.account_id is None:
            continue
        payload = SendMessagePayload.model_validate(action.payload or {})
        payload = _with_group_bot_admission_snapshot(
            session,
            task,
            int(action.account_id),
            payload,
        )
        if not payload.group_bot_admission_state:
            continue
        action.payload = payload.model_dump(mode="json")
        updated += 1
    return updated


def _canonicalized_task_config(session: Session, task: Task, config: dict) -> dict:
    """Apply legacy coverage defaults without changing a live target route."""
    del session
    source_config = {
        key: value
        for key, value in config.items()
        if key != "_daily_coverage_enforced"
    }
    daily_coverage_enforced = source_config.get("account_coverage_mode") == "all_accounts_daily"
    normalized = apply_group_ai_account_coverage_defaults(
        task.type,
        source_config,
        task.account_config or {},
    )
    if normalized != source_config:
        task.type_config = {key: value for key, value in normalized.items() if key != "pacing_config"}
    return {**normalized, "_daily_coverage_enforced": daily_coverage_enforced}


def _reply_targets_for_plan(
    session: Session,
    task: Task,
    group: TgGroup,
    usable_context_rows: list,
    turn_count: int,
    config: dict,
    hard_progress: dict[str, object],
    *,
    daily_coverage_debt: bool = False,
    daily_group_target_id: str = "",
) -> tuple[list[dict] | None, bool]:
    reply_target_pool = _group_reply_target_pool(
        session, task, group, usable_context_rows,
    )
    continuity_count = sum(
        1 for target in reply_target_pool
        if target.get("conversation_turn_claim_id")
    )
    reply_min = max(continuity_count, reply_requirement_for_plan(
        session,
        turn_count=turn_count,
        config=config,
        daily_group_target_id=daily_group_target_id,
    ))
    if reply_min <= 0:
        return [], False
    if reply_min > len(reply_target_pool):
        stats_inc(task, "reply_target_shortfall_count")
        if daily_coverage_debt or hard_progress:
            stats_inc(task, "coverage_reply_shortfall_cycle_count")
            return [], True
        task.last_error = "我方可引用消息不足，等待本任务产生可引用消息后继续执行"
        return None, False
    return reply_target_pool[:reply_min], False


def _daily_coverage_generation_is_deferred(
    session: Session,
    task: Task,
    group: TgGroup,
    *,
    usable_context_rows: list,
    turn_count: int,
    config: dict,
    hard_progress: dict[str, object],
    has_daily_coverage_debt: bool,
) -> bool:
    if hard_progress or not has_daily_coverage_debt or not _all_accounts_daily_coverage(config):
        return False
    reply_min = min(turn_count, int(config.get("reply_min_per_round") or 0))
    if reply_min <= 0:
        return True
    return len(_group_reply_target_pool(session, task, group, usable_context_rows)) < reply_min


def _hard_blocked_last_error(
    task: Task,
    *,
    created: int,
    blockers: dict[str, int],
    progress: dict[str, object],
) -> str:
    if task.last_error == HARD_HOURLY_GROUP_COOLDOWN_BLOCKED_MESSAGE:
        return task.last_error
    if created > 0 or not progress:
        return ""
    if blockers.get("account_capacity"):
        return ACCOUNT_CAPACITY_BLOCKED_MESSAGE
    return ""


def _hard_hourly_distribution_skew(account_ids: list[int], selected_account_count: int) -> dict[str, int]:
    planned_ids = [int(account_id) for account_id in account_ids if int(account_id or 0) > 0]
    if len(planned_ids) < HARD_HOURLY_MIN_DISTRIBUTION_ACTIONS:
        return {}
    if selected_account_count < HARD_HOURLY_MIN_DISTRIBUTED_ACCOUNTS:
        return {}
    unique_count = len(set(planned_ids))
    max_run = _max_consecutive_account_run(planned_ids)
    min_unique = min(HARD_HOURLY_MIN_DISTRIBUTED_ACCOUNTS, selected_account_count, len(planned_ids))
    if max_run <= HARD_HOURLY_MAX_CONSECUTIVE_ACCOUNT_RUN and unique_count >= min_unique:
        return {}
    return {"max_consecutive_run": max_run, "unique_account_count": unique_count}


def _max_consecutive_account_run(account_ids: list[int]) -> int:
    max_run = 0
    current_run = 0
    previous_id = None
    for account_id in account_ids:
        current_run = current_run + 1 if account_id == previous_id else 1
        max_run = max(max_run, current_run)
        previous_id = account_id
    return max_run


def _record_hard_hourly_distribution_skew(task: Task, skew: dict[str, int]) -> None:
    stats = dict(task.stats or {})
    stats["hard_hourly_distribution_skew"] = dict(skew)
    task.stats = stats
    task.last_error = ACCOUNT_DISTRIBUTION_SKEW_MESSAGE


def _choose_capacity_slot(
    session: Session,
    task: Task,
    *,
    selected: list,
    planned_at: datetime,
    index: int,
    used_account_ids: set[int],
    allow_repeat: bool,
    progress: dict[str, object],
    reservations: list[AccountCapacityReservation],
    capacity_cache: AccountCapacityCache,
) -> tuple[object | None, datetime]:
    if task.fulfillment_contract_version == "fact_first_v3":
        available = [item for item in selected if item.id not in used_account_ids]
        if not available and allow_repeat:
            available = list(selected)
        return (random.SystemRandom().choice(available), planned_at) if available else (None, planned_at)
    candidate_limit = _capacity_candidate_limit(used_account_ids)
    available = _available_accounts_at(session, task, selected, planned_at, reservations, capacity_cache, limit=candidate_limit)
    account = _choose_turn_account(available, available, index, used_account_ids, allow_repeat)
    if account:
        return account, planned_at
    decision = next_capacity_window(
        session,
        tenant_id=task.tenant_id,
        account_ids=[item.id for item in selected],
        scheduled_at=planned_at,
        reservations=reservations,
        cache=capacity_cache,
    )
    if not decision.defer_until or _defer_crosses_hard_hour(progress, decision.defer_until):
        return None, planned_at
    deferred_available = _available_accounts_at(session, task, selected, decision.defer_until, reservations, capacity_cache, limit=candidate_limit)
    account = _choose_turn_account(deferred_available, deferred_available, index, used_account_ids, allow_repeat)
    return (account, decision.defer_until) if account else (None, planned_at)


def _capacity_candidate_limit(used_account_ids: set[int]) -> int:
    return max(1, len(used_account_ids) + 1)


def _available_accounts_at(
    session: Session,
    task: Task,
    selected: list,
    scheduled_at: datetime,
    reservations: list[AccountCapacityReservation],
    capacity_cache: AccountCapacityCache,
    *,
    limit: int | None = None,
) -> list:
    return available_accounts_by_capacity(
        session,
        tenant_id=task.tenant_id,
        accounts=selected,
        scheduled_at=scheduled_at,
        reservations=reservations,
        cache=capacity_cache,
        limit=limit,
    )


def _defer_crosses_hard_hour(progress: dict[str, object], defer_until: datetime) -> bool:
    hour_end = progress.get("hour_end") if progress else None
    if not isinstance(hour_end, datetime):
        return False
    from app.services.task_center.datetime_compat import is_after_or_equal

    return is_after_or_equal(defer_until, hour_end)


def _select_accounts_for_plan(
    session: Session,
    task: Task,
    group: TgGroup,
    progress: dict[str, object],
    config: dict,
    coverage_rows: list[TaskAccountDailyCoverage] | None = None,
) -> list:
    options = _hard_hourly_account_options(progress)
    coverage_options = _daily_coverage_account_options(config)
    ready_rows = _ready_coverage_rows(task, config, coverage_rows)
    if _daily_coverage_enforced(config) and coverage_rows is None:
        ready_rows = ready_coverage_rows(session, task)
    candidate_account_ids = _candidate_account_ids_for_plan(
        session,
        task,
        config,
        ready_rows,
        progress,
    )
    if candidate_account_ids:
        options["limit"] = len(candidate_account_ids)
        options["enforce_max_concurrent"] = False
    return select_task_accounts(
        session,
        task.tenant_id,
        task.account_config or {},
        target_group_id=group.id,
        daily_coverage_task_id=task.id,
        daily_coverage_action_types=("send_message",),
        candidate_account_ids=candidate_account_ids,
        **coverage_options,
        **options,
    )


def _candidate_account_ids_for_plan(
    session: Session,
    task: Task,
    config: dict,
    ready_rows: list[TaskAccountDailyCoverage],
    _progress: dict[str, object],
) -> list[int] | None:
    if not _daily_coverage_enforced(config):
        return None
    if ready_rows:
        return [row.account_id for row in ready_rows]
    return []


def _plan_account_limit(
    task: Task,
    progress: dict[str, object],
    *,
    planning_limit: int | None = None,
) -> int:
    limit = (
        _hard_hourly_account_scan_target(progress)
        if progress
        else max(1, int((task.account_config or {}).get("max_concurrent") or 20))
    )
    if _all_accounts_daily_coverage(task.type_config or {}):
        transaction_limit = MAX_DAILY_COVERAGE_PLAN_BATCH
        if planning_limit is not None:
            transaction_limit = min(transaction_limit, max(1, int(planning_limit)))
        if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
            # Current capacity is narrowed by due/candidates, not the legacy task field.
            return transaction_limit
        return min(limit, transaction_limit)
    return limit


def _ready_coverage_rows(
    task: Task,
    config: dict,
    coverage_rows: list[TaskAccountDailyCoverage] | None,
) -> list[TaskAccountDailyCoverage]:
    if not _daily_coverage_enforced(config) or coverage_rows is None:
        return []
    plannable_states = (
        {"ready", "pending_admission"}
        if task.fulfillment_contract_version == "fact_first_v3"
        else {"ready"}
    )
    return [
        row for row in coverage_rows
        if row.state in plannable_states and row.confirmed_count < row.target_count
    ]


def _online_ready_accounts(session: Session, task: Task, accounts: list, progress: dict[str, object]) -> list:
    ready_ids = online_ready_account_ids_for_planning(
        session,
        tenant_id=task.tenant_id,
        accounts=accounts,
        now=_now(),
    )
    ready = [account for account in accounts if account.id in ready_ids]
    selected_ids = [account.id for account in accounts]
    ordered_ready_ids = [account.id for account in ready]
    _record_online_ready_stats(task, selected_ids, ordered_ready_ids, progress)
    if _all_accounts_daily_coverage(getattr(task, "type_config", {}) or {}):
        block_coverage_accounts(
            session,
            task,
            [account_id for account_id in selected_ids if account_id not in ready_ids],
            blocker_code="account_offline",
            blocker_detail="账号实时在线状态不可用",
            next_eligible_at=_now() + timedelta(minutes=5),
        )
    return ready


def _record_online_ready_stats(
    task: Task,
    selected_account_ids: list[int],
    ready_account_ids: list[int],
    progress: dict[str, object],
) -> None:
    ready_set = set(ready_account_ids)
    offline_ids = [account_id for account_id in selected_account_ids if account_id not in ready_set]
    offline_count = len(offline_ids)
    stats = dict(task.stats or {})
    if offline_count:
        stats["account_online_selected_count"] = len(selected_account_ids)
        stats["account_online_ready_count"] = len(ready_account_ids)
        stats["account_offline_count"] = offline_count
        stats["account_offline_sample_account_ids"] = offline_ids[:ACCOUNT_OFFLINE_SAMPLE_LIMIT]
        task.stats = stats
        if progress:
            progress["account_offline_count"] = offline_count
        return
    stats.pop("account_online_selected_count", None)
    stats.pop("account_online_ready_count", None)
    stats.pop("account_offline_count", None)
    stats.pop("account_offline_sample_account_ids", None)
    task.stats = stats


def _daily_coverage_uncovered_count(
    session: Session,
    task: Task,
    accounts: list,
    progress: dict[str, object],
    config: dict,
    coverage_state: CoveragePlanState | None = None,
) -> int:
    if not _all_accounts_daily_coverage(config):
        uncovered = daily_uncovered_account_count(session, task.id, ("send_message",), accounts)
        return min(uncovered, max(0, int(progress.get("deficit") or 0))) if progress else uncovered
    hard_deficit = max(0, int(progress.get("deficit") or 0)) if progress else 0
    daily_debt = (
        max(
            0,
            int(coverage_state.due_debt or 0),
            int(coverage_state.volume_need_now or 0),
        )
        if coverage_state
        else 0
    )
    return min(len(accounts), max(hard_deficit, daily_debt))


def _daily_coverage_account_options(config: dict) -> dict[str, object]:
    if not _daily_coverage_enforced(config):
        return {}
    return {
        "daily_coverage_target_count": _coverage_target_per_account(config),
        "daily_coverage_statuses": DAILY_COVERAGE_SUCCESS_STATUSES,
    }


def _all_accounts_daily_coverage(config: dict) -> bool:
    return config.get("account_coverage_mode") == "all_accounts_daily"


def _daily_coverage_enforced(config: dict) -> bool:
    return _all_accounts_daily_coverage(config) and bool(config.get("_daily_coverage_enforced", True))


def _hard_hourly_group_cooldown_blocker(
    task: Task,
    group: TgGroup,
    progress: dict[str, object],
) -> dict[str, object]:
    if not progress:
        _clear_hard_hourly_group_cooldown_blocker(task)
        return {}
    # Gate against one-hour planning rate, never the full multi-hour backfill debt.
    proof = hard_hourly_group_cooldown_proof(
        group=group,
        hourly_target=int(progress.get("goal") or 0),
        backfill_planning_deficit=int(progress.get("backfill_planning_deficit") or 0),
        required_hourly_messages=hard_hourly_planning_rate(progress),
    )
    if proof["sufficient"]:
        _clear_hard_hourly_group_cooldown_blocker(task)
        return {}
    task.stats = {
        **(task.stats or {}),
        "hard_hourly_capacity_status": "blocked",
        "hard_hourly_capacity_proof": proof,
    }
    task.last_error = HARD_HOURLY_GROUP_COOLDOWN_BLOCKED_MESSAGE
    _mark_hard_blocked(task, progress, str(proof["blocker_code"]))
    return proof


def _clear_hard_hourly_group_cooldown_blocker(task: Task) -> None:
    stats = dict(task.stats or {})
    stats.pop("hard_hourly_capacity_status", None)
    stats.pop("hard_hourly_capacity_proof", None)
    task.stats = stats
    if task.last_error == HARD_HOURLY_GROUP_COOLDOWN_BLOCKED_MESSAGE:
        task.last_error = ""


def _coverage_capacity_blocker(
    session: Session,
    task: Task,
    group: TgGroup,
    config: dict,
    coverage_rows: list[TaskAccountDailyCoverage] | None = None,
    coverage_state: CoveragePlanState | None = None,
) -> dict[str, object]:
    del session, group, config, coverage_rows, coverage_state
    stats = dict(task.stats or {})
    stats.pop("coverage_capacity_status", None)
    stats.pop("coverage_capacity_proof", None)
    stats.pop("sendable_coverage_capacity_proof", None)
    task.stats = stats
    if task.last_error in {
        ALL_ACCOUNT_COVERAGE_CAPACITY_BLOCKED_MESSAGE,
        SENDABLE_COVERAGE_CAPACITY_BLOCKED_MESSAGE,
    }:
        task.last_error = ""
    return {}


def _coverage_plan_state(
    session: Session,
    task: Task,
    group: TgGroup,
    *,
    config: dict,
    progress: dict[str, object],
) -> CoveragePlanState:
    timestamp = _now()
    if not _all_accounts_daily_coverage(config):
        ensure_task_day_ledger(session, task, now=timestamp)
        return CoveragePlanState(rows=[], rows_by_account={}, due_debt=0)
    ledger, participation, admission, account_ids = _coverage_scope(
        session, task, group, timestamp=timestamp)
    ensure_task_daily_coverage(
        session, task, now=timestamp, account_ids=account_ids,
        target_group=group, refresh_existing=True,
    )
    ledger = ensure_task_day_ledger(session, task, now=timestamp)
    bind_unowned_group_slots_to_coverage(session, task, ledger, group)
    if bool(config.get("allow_mask_missing_check_in", False)):
        release_voice_profile_coverage_for_check_in(session, task, now=timestamp)
    backfill_daily_coverage_confirmations(session, task, timestamp.date())
    totals = coverage_plan_totals(session, task, group, now=timestamp)
    target, due_message_count, volume_need = _daily_group_due_state(
        session,
        task,
        group,
        timestamp=timestamp,
    )
    if admission is not None:
        target.planning_admission_snapshot_id = admission.id
    rows = _coverage_candidate_rows(
        session, task, group=group, ledger=ledger, target=target,
        participation=participation, admission=admission, timestamp=timestamp,
        required_units=max(volume_need, totals.due_debt),
    )
    effective_due_debt = _effective_coverage_due_debt(
        task,
        coverage_due_debt=totals.due_debt,
        volume_need=volume_need,
    )
    return _build_coverage_plan_state(
        rows,
        totals=totals,
        target=target,
        due_message_count=due_message_count,
        volume_need=volume_need,
        effective_due_debt=effective_due_debt,
        deadline_at=ledger.deadline_at,
    )


def _coverage_scope(
    session: Session,
    task: Task,
    group: TgGroup,
    *,
    timestamp: datetime,
):
    ledger = ensure_task_day_ledger(session, task, now=timestamp)
    if not _uses_unified_engagement(task):
        bootstrap_missing_all_account_task_scope(session, task, now=timestamp)
        return ledger, None, None, None
    participation = ensure_daily_participation_plan(session, task, ledger)
    account_ids = [int(item) for item in participation.selected_account_ids]
    sync_group_participation_scope(session, task, group, account_ids=account_ids)
    admission = ensure_planning_admission_snapshot(
        session,
        task,
        participation,
        planning_horizon=f"task_day:{ledger.obligation_local_date.isoformat()}",
        target=group_operation_target(session, task, group),
        require_send=True,
    )
    return ledger, participation, admission, account_ids


def _coverage_candidate_rows(
    session: Session,
    task: Task,
    *,
    group: TgGroup,
    ledger,
    target: TaskGroupDailyTarget,
    participation,
    admission,
    timestamp: datetime,
    required_units: int,
) -> list[TaskAccountDailyCoverage]:
    rows = _ready_coverage_rows_for_plan(session, task, timestamp=timestamp)
    if admission is not None:
        admissible = {int(item) for item in admission.admissible_account_ids or []}
        rows = [row for row in rows if int(row.account_id) in admissible]
    rows = _portfolio_coverage_rows(
        session, task, ledger=ledger, target=target,
        participation=participation, rows=rows,
    )
    if participation is None or not _uses_unified_engagement(task):
        return rows
    opportunity = ensure_natural_opportunity_plan(
        session, task, ledger, group=group, required_units=required_units,
    )
    return rows[:opportunity.guaranteed_now_capacity]


def _uses_unified_engagement(task: Task) -> bool:
    return str(
        (task.type_config or {}).get("engagement_contract_version") or ""
    ) == "unified_engagement_v1"


def _portfolio_coverage_rows(
    session: Session,
    task: Task,
    *,
    ledger,
    target: TaskGroupDailyTarget,
    participation,
    rows: list[TaskAccountDailyCoverage],
) -> list[TaskAccountDailyCoverage]:
    if participation is None or not _uses_unified_engagement(task):
        return rows
    decision = reserve_portfolio_units(
        session,
        task,
        ledger,
        action_class="authored_message",
        demand_identity=f"group_daily:{target.id}",
        total_units=int(target.effective_message_target or 0),
        candidate_account_ids=[
            int(item) for item in participation.selected_account_ids or []
        ],
    )
    allowed = set(decision.allocated_units_by_account)
    return [row for row in rows if int(row.account_id) in allowed]


def _daily_group_due_state(
    session: Session,
    task: Task,
    group: TgGroup,
    *,
    timestamp: datetime,
) -> tuple[TaskGroupDailyTarget, int, int]:
    target = ensure_task_group_daily_target(
        session,
        task,
        group,
        timestamp.date(),
        now=timestamp,
    )
    due_message_count = daily_group_due_message_count(
        target,
        task.pacing_config or {},
        anchor_at=task_pacing_anchor(task),
        now=timestamp,
    )
    target.due_message_count = due_message_count
    volume_need = max(
        0,
        due_message_count
        - target.confirmed_message_count
        - _valid_open_daily_send_count(session, task, target.task_day_ledger_id),
    )
    record_daily_fulfillment_decision(
        session,
        task,
        reason="planner_evaluated",
        hard_hourly_required_new=0,
        now=timestamp,
    )
    return target, due_message_count, volume_need


def _ready_coverage_rows_for_plan(
    session: Session,
    task: Task,
    *,
    timestamp: datetime,
) -> list[TaskAccountDailyCoverage]:
    configured_limit = int(getattr(get_settings(), "daily_coverage_plan_batch_limit", 20) or 20)
    return ready_coverage_plan_batch(
        session,
        task,
        now=timestamp,
        limit=configured_limit,
    ).rows


def _build_coverage_plan_state(
    rows: list[TaskAccountDailyCoverage],
    *,
    totals,
    target: TaskGroupDailyTarget,
    due_message_count: int,
    volume_need: int,
    effective_due_debt: int,
    deadline_at: datetime,
) -> CoveragePlanState:
    return CoveragePlanState(
        rows=rows,
        rows_by_account={row.account_id: row for row in rows},
        due_debt=effective_due_debt,
        account_count=totals.account_count,
        target_per_account=totals.target_per_account,
        confirmed_count=totals.confirmed_count,
        reserved_count=totals.reserved_count,
        sendable_account_count=totals.sendable_account_count,
        sendable_confirmed_count=totals.sendable_confirmed_count,
        sendable_reserved_count=totals.sendable_reserved_count,
        required_new=max(effective_due_debt, volume_need),
        daily_group_target_id=target.id,
        effective_daily_target=target.effective_message_target,
        due_message_count=due_message_count,
        confirmed_message_count=target.confirmed_message_count,
        volume_need_now=volume_need,
        deadline_at=deadline_at,
    )


def _effective_coverage_due_debt(
    task: Task,
    *,
    coverage_due_debt: int,
    volume_need: int,
) -> int:
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        return max(0, int(volume_need))
    return max(0, int(coverage_due_debt))


def _valid_open_daily_send_count(
    session: Session,
    task: Task,
    task_day_ledger_id: str | None = None,
) -> int:
    statement = select(func.count(Action.id)).where(
        Action.tenant_id == task.tenant_id,
        Action.task_id == task.id,
        Action.action_type == "send_message",
        Action.status.in_(("pending", "claiming", "executing", "unknown_after_send")),
    )
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        if not task_day_ledger_id:
            return 0
        statement = statement.join(
            TaskGroupDailyMessageSlot,
            TaskGroupDailyMessageSlot.id == Action.primary_quantity_slot_id,
        ).where(
            TaskGroupDailyMessageSlot.task_day_ledger_id == task_day_ledger_id,
            TaskGroupDailyMessageSlot.state.in_(("open", "unknown")),
            TaskGroupDailyMessageSlot.quantity_credit_eligible.is_(True),
        )
        return int(session.scalar(statement) or 0)
    config = task.type_config if isinstance(task.type_config, dict) else {}
    required = _group_bot_admission_requirement(config)
    group_id = int(config.get("target_group_id") or 0)
    if required is not False and group_id:
        statement = statement.outerjoin(
            GroupBotAdmission,
            and_(
                GroupBotAdmission.tenant_id == Action.tenant_id,
                GroupBotAdmission.group_id == group_id,
                GroupBotAdmission.account_id == Action.account_id,
            ),
        )
        ready = GroupBotAdmission.state.in_(GROUP_BOT_PLANNABLE_STATES)
        statement = statement.where(
            ready if required is True else or_(GroupBotAdmission.id.is_(None), ready),
        )
    return int(session.scalar(statement) or 0)


def _group_bot_admission_requirement(config: dict) -> bool | None:
    configured = config.get("group_bot_admission_required")
    if configured is not None:
        return bool(configured)
    if _all_accounts_daily_coverage(config):
        return True
    return None


def requires_planning_with_open_actions(session: Session, task: Task) -> bool:
    config = task.type_config or {}
    if not _all_accounts_daily_coverage(config):
        return False
    group = group_from_reference(
        session,
        task.tenant_id,
        group_id=int(config.get("target_group_id") or 0) or None,
        operation_target_id=int(config.get("target_operation_target_id") or 0) or None,
        require_authorized=False,
    )
    if not group:
        return False
    summary = record_daily_fulfillment_decision(session, task, reason="open_actions_gate", now=_now())
    target = ensure_task_group_daily_target(
        session,
        task,
        group,
        _now().date(),
        now=_now(),
    )
    due = daily_group_due_message_count(
        target,
        task.pacing_config or {},
        anchor_at=task_pacing_anchor(task),
        now=_now(),
    )
    volume_need = max(
        0,
        due
        - target.confirmed_message_count
        - _valid_open_daily_send_count(session, task, target.task_day_ledger_id),
    )
    return summary.ready_to_plan_count > 0 or volume_need > 0


def _record_daily_coverage_next_check(task: Task, has_debt: bool) -> None:
    stats = dict(task.stats or {})
    if has_debt:
        stats["daily_coverage_next_check_at"] = (
            _now() + timedelta(seconds=DAILY_COVERAGE_DEBT_RECHECK_SECONDS)
        ).isoformat()
    else:
        stats.pop("daily_coverage_next_check_at", None)
    task.stats = stats


def _load_coverage_rows(
    session: Session,
    task: Task,
    *,
    now: datetime | None = None,
) -> list[TaskAccountDailyCoverage]:
    timestamp = now or _now()
    return list(session.scalars(select(TaskAccountDailyCoverage).where(
        TaskAccountDailyCoverage.task_id == task.id,
        TaskAccountDailyCoverage.coverage_date == timestamp.date(),
    )))


def _coverage_round_config(config: dict, _progress: dict[str, object]) -> dict:
    if _daily_coverage_enforced(config):
        return {**config, "allow_account_repeat": False}
    return config


def _coverage_target_per_account(config: dict) -> int:
    try:
        value = int(config.get("per_account_daily_min_messages") or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(2, value))


def _coverage_counts_for_plan(
    accounts: list,
    config: dict,
    rows_by_account: dict[int, TaskAccountDailyCoverage],
) -> dict[int, int]:
    if not _all_accounts_daily_coverage(config):
        return {}
    return {
        int(account.id): rows_by_account[int(account.id)].confirmed_count
        for account in accounts if int(account.id) in rows_by_account
    }


def _coverage_rows_for_plan(
    accounts: list,
    config: dict,
    rows_by_account: dict[int, TaskAccountDailyCoverage],
) -> dict[int, TaskAccountDailyCoverage]:
    if not _all_accounts_daily_coverage(config):
        return {}
    return {
        int(account.id): rows_by_account[int(account.id)]
        for account in accounts if int(account.id) in rows_by_account
    }


def _coverage_payload_for_account(
    config: dict,
    account_id: int,
    counts: dict[int, int],
    rows: dict[int, TaskAccountDailyCoverage],
) -> dict[str, object]:
    if not _all_accounts_daily_coverage(config):
        return {}
    target = _coverage_target_per_account(config)
    completed = max(0, int(counts.get(int(account_id), 0)))
    remaining = max(0, target - completed)
    row = rows.get(int(account_id))
    return {
        "account_coverage_mode": "all_accounts_daily",
        "coverage_window_date": _now().date().isoformat(),
        "coverage_target_per_account": target,
        "coverage_account_completed_before_action": completed,
        "coverage_account_remaining_before_action": remaining,
        "coverage_reason": "daily_account_coverage" if remaining else "",
        "coverage_ledger_id": row.id if row and remaining else "",
    }


def _accounts_with_ready_voice_profiles(accounts: list, voice_profiles: dict[int, dict[str, str | int]]) -> tuple[list, list[int]]:
    ready_accounts = []
    missing_ids: list[int] = []
    for account in accounts:
        if _voice_profile_ready(voice_profiles.get(int(account.id))):
            ready_accounts.append(account)
        else:
            missing_ids.append(int(account.id))
    return ready_accounts, missing_ids


def _profile_ready_accounts_for_plan(
    session: Session,
    task: Task,
    *,
    group: TgGroup,
    progress: dict[str, object],
    config: dict,
    accounts: list,
    coverage_rows: list[TaskAccountDailyCoverage] | None = None,
) -> tuple[list, dict[int, dict[str, str | int]], list[int]]:
    initial_count = len(accounts)
    scanned_count = len(accounts)
    while True:
        voice_profiles = voice_profile_prompt_details(
            session,
            tenant_id=task.tenant_id,
            account_ids=[account.id for account in accounts],
        )
        ready_accounts, missing_ids = _accounts_with_ready_voice_profiles(accounts, voice_profiles)
        if not _needs_voice_profile_refill(progress, config, ready_accounts, missing_ids):
            _record_voice_profile_refill(task, initial_count, len(accounts))
            return ready_accounts, voice_profiles, missing_ids
        next_target = _voice_profile_refill_target(scanned_count, ready_accounts, missing_ids)
        expanded_accounts = _select_accounts_for_plan(
            session,
            task,
            group,
            {**progress, "account_scan_target": next_target},
            config,
            coverage_rows=coverage_rows,
        )
        if len(expanded_accounts) <= scanned_count:
            _record_voice_profile_refill(task, initial_count, len(accounts))
            return ready_accounts, voice_profiles, missing_ids
        scanned_count = len(expanded_accounts)
        accounts = _online_ready_accounts(session, task, expanded_accounts, progress)


def _needs_voice_profile_refill(
    progress: dict[str, object],
    config: dict,
    ready_accounts: list,
    missing_ids: list[int],
) -> bool:
    required = _hard_hourly_batch_size(config, progress) if progress else 0
    return bool(progress and missing_ids and len(ready_accounts) < required)


def _voice_profile_refill_target(
    scanned_count: int,
    ready_accounts: list,
    missing_ids: list[int],
) -> int:
    shortfall = max(0, scanned_count - len(ready_accounts))
    refill_count = max(shortfall, len(missing_ids))
    return scanned_count + max(1, refill_count)


def _record_voice_profile_refill(task: Task, initial_count: int, final_count: int) -> None:
    if final_count <= initial_count:
        return
    stats = dict(task.stats or {})
    stats["voice_profile_refill_account_count"] = final_count - initial_count
    task.stats = stats


def _voice_profile_ready(profile: dict[str, str | int] | None) -> bool:
    if not profile:
        return False
    return int(profile.get("version") or 0) > 0 and bool(str(profile.get("summary") or "").strip())


def _queue_missing_voice_profile_recovery(
    session: Session,
    task: Task,
    config: dict,
    account_ids: list[int],
) -> datetime | None:
    result = enqueue_voice_profile_generation(
        session,
        tenant_id=task.tenant_id,
        account_ids=account_ids,
        source="task_precheck",
        actor="task-planner",
        reason="AI 活跃群发现账号面具待恢复",
    )
    if result.next_retry_at is not None:
        task.next_run_at = result.next_retry_at
    return result.next_retry_at


def _record_missing_voice_profiles(session: Session, task: Task, account_ids: list[int]) -> None:
    stats_inc(task, "skipped_count", len(account_ids))
    stats = dict(task.stats or {})
    observed = int(stats.get("voice_profile_missing_observation_count") or 0) + len(account_ids)
    stats["voice_profile_missing_observation_count"] = observed
    stats["voice_profile_missing_account_count"] = _current_missing_voice_profile_account_count(session, task, account_ids)
    counts = dict(stats.get("quality_rejection_counts") or {})
    counts["voice_profile_missing"] = int(counts.get("voice_profile_missing") or 0) + len(account_ids)
    stats["quality_rejection_counts"] = counts
    stats["quality_rejection_samples"] = _missing_voice_profile_samples(stats, account_ids)
    task.stats = stats


def _current_missing_voice_profile_account_count(
    session: Session,
    task: Task,
    account_ids: list[int],
) -> int:
    if not _all_accounts_daily_coverage(task.type_config or {}):
        return len(set(account_ids))
    return int(session.scalar(
        select(func.count(TaskAccountDailyCoverage.account_id)).where(
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.coverage_date == _now().date(),
            TaskAccountDailyCoverage.state == "blocked",
            TaskAccountDailyCoverage.blocker_code == VOICE_PROFILE_MISSING_BLOCKER_CODE,
        )
    ) or 0)


def _missing_voice_profile_samples(stats: dict[str, object], account_ids: list[int]) -> list[dict[str, object]]:
    samples = list(stats.get("quality_rejection_samples") or [])
    existing = sum(1 for item in samples if str(item.get("reason") or "") == "voice_profile_missing")
    for account_id in account_ids:
        if existing >= QUALITY_REJECTION_SAMPLE_LIMIT:
            break
        samples.append({"reason": "voice_profile_missing", "content": "", "status": "blocked", "account_id": account_id, "detail": VOICE_PROFILE_MISSING_MESSAGE})
        existing += 1
    return samples


def _account_prompt_profiles(
    account_profiles: dict[str, str],
    voice_profiles: dict[int, dict[str, Any]],
    stance_summaries: dict[int, str],
) -> dict[str, str]:
    account_ids = set(int(account_id) for account_id in account_profiles.keys())
    account_ids.update(voice_profiles.keys())
    account_ids.update(stance_summaries.keys())
    result: dict[str, str] = {}
    for account_id in account_ids:
        parts = [
            str(account_profiles.get(str(account_id)) or "").strip(),
            _voice_profile_prompt_text(voice_profiles.get(account_id) or {}),
            f"短期立场：{stance_summaries[account_id]}" if stance_summaries.get(account_id) else "",
        ]
        result[str(account_id)] = "；".join(part for part in parts if part)
    return result


def _voice_profile_prompt_text(profile: dict[str, Any]) -> str:
    tags = _voice_profile_tags(profile.get("preference_tags"))
    parts = [
        f"面具：{profile['mask_name']}" if str(profile.get("mask_name") or "").strip() else "",
        f"人群：{profile['audience_archetype']}" if str(profile.get("audience_archetype") or "").strip() else "",
        f"服务角色参考（非身份事实）：{profile['identity_frame']}" if str(profile.get("identity_frame") or "").strip() else "",
        f"偏好：{'、'.join(tags)}" if tags else "",
        f"表达摘要：{profile['summary']}" if str(profile.get("summary") or "").strip() else "",
    ]
    return "；".join(part for part in parts if part)


def _voice_profile_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _generation_slots_for_plan(
    *,
    cycle_id: str,
    accounts: list,
    turn_count: int,
    reply_targets: list[dict],
    account_prompt_profiles: dict[str, str],
    allow_account_repeat: bool,
    burst_plan: dict[int, dict] | None = None,
    burst_account=None,
    topic_directions: list[dict] | None = None,
    teacher_targets: list[dict] | None = None,
    recent_topic_counts: dict[str, int] | None = None,
    recent_teacher_counts: dict[str, int] | None = None,
    is_generic_warmup: bool = False,
) -> list[dict]:
    slots: list[dict] = []
    topics, teachers = _generation_target_sequences(
        turn_count,
        topic_directions=topic_directions,
        teacher_targets=teacher_targets,
        recent_topic_counts=recent_topic_counts,
        recent_teacher_counts=recent_teacher_counts,
    )
    for index in range(max(0, int(turn_count or 0))):
        account = (
            burst_account
            if burst_plan and index in burst_plan and burst_account
            else _slot_account(accounts, index, allow_account_repeat)
        )
        if not account:
            break
        reply_target = reply_targets[index] if index < len(reply_targets) else None
        slots.append(
            _generation_slot(
                cycle_id,
                index,
                account,
                reply_target,
                account_prompt_profiles,
                _slot_target(topics, index),
                _slot_target(teachers, index),
                is_generic_warmup=is_generic_warmup,
            )
        )
    return slots


def _generation_target_sequences(
    turn_count: int,
    *,
    topic_directions: list[dict] | None,
    teacher_targets: list[dict] | None,
    recent_topic_counts: dict[str, int] | None,
    recent_teacher_counts: dict[str, int] | None,
) -> tuple[list[dict], list[dict]]:
    topics = _conversation_target_sequence(
        topic_directions or [], turn_count, label_key="title", rank_key="weight",
        recent_counts=recent_topic_counts,
    )
    teachers = _conversation_target_sequence(
        teacher_targets or [], turn_count, label_key="name", rank_key="priority",
        recent_counts=recent_teacher_counts,
    )
    return topics, teachers


def _slot_account(accounts: list, index: int, allow_account_repeat: bool):
    if not accounts:
        return None
    if allow_account_repeat:
        return accounts[index % len(accounts)]
    return accounts[index] if index < len(accounts) else None


def _generation_slot(
    cycle_id: str,
    index: int,
    account,
    reply_target: dict | None,
    profiles: dict[str, str],
    topic_direction: dict | None = None,
    teacher_target: dict | None = None,
    *,
    is_generic_warmup: bool = False,
) -> dict:
    quality_item = {"reply_target": reply_target} if reply_target else {}
    content = str((reply_target or {}).get("content") or "")
    profile = profiles.get(str(account.id), "")
    act_type = (
        "question" if is_generic_warmup else _act_type_for_turn(index, quality_item)
    )
    slot = {
        "slot_id": _slot_id(cycle_id, index),
        "sequence_index": index + 1,
        "account_id": account.id,
        "act_type": act_type,
        "stance": _stance_for_act_type(act_type),
        "account_profile": profile,
        "reply_to_message_id": _reply_target_message_id(quality_item),
        "reply_to_content": content,
    }
    if topic_direction:
        slot["topic_direction"] = dict(topic_direction)
    if teacher_target:
        slot["teacher_target"] = dict(teacher_target)
    return slot


def _stance_for_act_type(act_type: str) -> str:
    if act_type == "light_disagree":
        return "reserved"
    if act_type == "short_react":
        return "positive"
    return "neutral"


def _conversation_target_sequence(
    items: list[dict],
    count: int,
    *,
    label_key: str,
    rank_key: str,
    recent_counts: dict[str, int] | None = None,
) -> list[dict]:
    candidates = [dict(item) for item in items if str(item.get(label_key) or "").strip()]
    usage = {
        _normalize_conversation_label(str(item.get(label_key) or "")): _usage_count(
            item,
            recent_counts or {},
            label_key,
        )
        for item in candidates
    }
    sequence: list[dict] = []
    for _index in range(max(0, int(count or 0))):
        target = _next_conversation_target(candidates, usage, label_key=label_key, rank_key=rank_key)
        if not target:
            break
        sequence.append(dict(target))
        _increment_usage(usage, _normalize_conversation_label(str(target.get(label_key) or "")))
    return sequence


def _next_conversation_target(
    candidates: list[dict],
    usage: dict[str, int],
    *,
    label_key: str,
    rank_key: str,
) -> dict | None:
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (
            int(usage.get(_normalize_conversation_label(str(item.get(label_key) or "")), 0) or 0),
            -float(item.get(rank_key) or 1),
        ),
    )[0]


def _slot_target(targets: list[dict], index: int) -> dict | None:
    return targets[index] if 0 <= index < len(targets) else None


def _generation_slots(config: dict) -> list[dict]:
    slots = config.get("generation_slots") if isinstance(config.get("generation_slots"), list) else []
    return [dict(slot) for slot in slots if isinstance(slot, dict)]


def _quality_slot(quality_item: dict) -> dict:
    slot = quality_item.get("slot") if isinstance(quality_item, dict) else {}
    return dict(slot) if isinstance(slot, dict) else {}


def _quality_slot_id(quality_item: dict) -> str:
    return str(quality_item.get("slot_id") or _quality_slot(quality_item).get("slot_id") or "")


def _quality_slot_account_id(quality_item: dict) -> int:
    value = quality_item.get("slot_account_id") or _quality_slot(quality_item).get("account_id")
    return int(value or 0) if str(value or "").isdigit() else 0


def _slot_id(cycle_id: str, index: int) -> str:
    return f"{cycle_id}:turn:{index + 1}"


def _act_type_for_turn(index: int, quality_item: dict) -> str:
    if quality_item.get("quality_fallback") == "check_in_fallback":
        return "check_in_fallback"
    if quality_item.get("act_type"):
        return canonical_ai_group_act_type(str(quality_item.get("act_type")))
    if _reply_target_message_id(quality_item) is not None:
        return "context_reply"
    act_types = ("short_react", "detail_follow", "question", "light_disagree", "topic_shift")
    return act_types[index % len(act_types)]


def _material_intent_for_turn(quality_item: dict) -> str:
    if not isinstance(quality_item, dict) or quality_item.get("allow_material") is False:
        return ""
    return str(quality_item.get("material_intent") or "").strip()


def _reserve_planned_message_memory(
    session: Session,
    task: Task,
    group: TgGroup,
    account_id: int,
    content: str,
    config: dict,
    profile_preview: dict,
    quality_item: dict,
):
    if not content.strip():
        return None
    topic = _quality_topic_direction(quality_item, config)
    teacher = _quality_teacher_target(quality_item, config)
    return reserve_group_ai_message(
        session,
        tenant_id=task.tenant_id,
        group_id=group.id,
        task_id=task.id,
        account_id=account_id,
        raw_text=content,
        topic_direction=_topic_target_text(topic, group),
        teacher_target=_teacher_target_text(teacher),
        profile_version=int(profile_preview.get("profile_version") or 0) or None,
        profile_match_score=_profile_match_score(profile_preview),
        profile_match_reason=_profile_match_reason(profile_preview),
    )


def _profile_match_score(profile_preview: dict) -> int:
    if str(profile_preview.get("profile_hit_summary") or "").strip():
        return ACTIVE_PROFILE_MATCH_SCORE
    return UNAVAILABLE_PROFILE_MATCH_SCORE


def _profile_match_reason(profile_preview: dict) -> str:
    hit_summary = str(profile_preview.get("profile_hit_summary") or "").strip()
    if hit_summary:
        return hit_summary
    return str(profile_preview.get("profile_unavailable_reason") or "profile_unavailable").strip()


def _increment_coverage_count(config: dict, account_id: int, counts: dict[int, int]) -> None:
    if _all_accounts_daily_coverage(config):
        counts[int(account_id)] = int(counts.get(int(account_id), 0)) + 1


def _account_shortage_reason(
    session: Session,
    task: Task,
    group: TgGroup,
    progress: dict[str, object],
    *,
    config: dict[str, object] | None = None,
) -> tuple[str, str]:
    # Use plan-time config with `_daily_coverage_enforced`. Scope bootstrap may persist
    # implicit all_accounts_daily defaults onto task.type_config without enabling the ledger gate.
    plan_config = config if isinstance(config, dict) else (task.type_config or {})
    if _daily_coverage_enforced(plan_config):
        if int((task.stats or {}).get("account_offline_count") or 0) > 0:
            return "账号在线状态不可用，等待账号恢复在线后继续执行", "account_offline"
        return "当日覆盖账本暂无可执行账号，等待阻塞恢复或冷却到期", "coverage_waiting"
    options = _hard_hourly_account_options(progress)
    if _has_account_candidate(session, task, group, task.account_config or {}, options):
        return ACCOUNT_CAPACITY_BLOCKED_MESSAGE, "account_capacity"
    no_cooldown_config = dict(task.account_config or {})
    no_cooldown_config["cooldown_per_account_minutes"] = 0
    if _has_account_candidate(session, task, group, no_cooldown_config, options):
        return ACCOUNT_COOLDOWN_BLOCKED_MESSAGE, "account_cooldown"
    return ACCOUNT_UNAVAILABLE_MESSAGE, "account_unavailable"


def _has_account_candidate(
    session: Session,
    task: Task,
    group: TgGroup,
    account_config: dict,
    options: dict[str, object],
) -> bool:
    return bool(
        select_task_accounts(
            session,
            task.tenant_id,
            account_config,
            target_group_id=group.id,
            enforce_capacity=True,
            **options,
        )
    )


def _hard_hourly_account_options(progress: dict[str, object]) -> dict[str, object]:
    if not progress:
        return {}
    return {
        "limit": _hard_hourly_account_scan_target(progress),
        # Continuity PRD: hard-hourly planning must respect account concurrency/capacity.
        "enforce_max_concurrent": True,
    }


def _hard_hourly_account_scan_target(progress: dict[str, object]) -> int:
    requested = max(0, int(progress.get("account_scan_target") or 0))
    if requested:
        return requested
    goal = max(0, int(progress.get("goal") or 0))
    deficit = max(0, int(progress.get("deficit") or 0))
    return max(HARD_HOURLY_MIN_BATCH_MESSAGES, goal, deficit)


def _deferred_ai_planned_items(
    reply_targets: list[dict],
    normal_count: int,
    slots: list[dict],
) -> list[dict]:
    items: list[dict] = []
    targets: list[dict | None] = [*reply_targets, *([None] * max(0, int(normal_count or 0)))]
    for index, target in enumerate(targets):
        item = {"content": "", "reply_target": target, "defer_ai_generation": True}
        slot = slots[index] if 0 <= index < len(slots) else None
        if slot:
            item.update(_deferred_slot_metadata(slot))
        items.append(item)
    return items


def _deferred_slot_metadata(slot: dict) -> dict:
    return {
        "slot": dict(slot),
        "slot_id": str(slot.get("slot_id") or ""),
        "act_type": canonical_ai_group_act_type(str(slot.get("act_type") or "")),
    }


def _deferred_ai_history(history: str) -> str:
    value = str(history or "").strip()
    return value[-DEFERRED_AI_HISTORY_MAX_CHARS:] if value else ""


def _provider_generation_payload(item: dict) -> dict[str, object]:
    keys = (
        "requested_model", "actual_model", "fallback_stage", "fallback_reason",
        "provider_duration_ms", "generation_attempts",
    )
    return {key: item[key] for key in keys if key in item}


def _group_reply_target_pool(session: Session, task: Task, group: TgGroup, rows: list) -> list[dict]:
    if _uses_unified_engagement(task):
        from ..engagement_conversation import interaction_reply_targets

        listener_targets = _admitted_continuity_targets(
            session, task, interaction_reply_targets(session, task, group, context_rows=rows),
        )
    else:
        listener_targets = _listener_context_reply_targets(rows)
    targets = [*listener_targets, *_historical_group_reply_targets(session, task, group)]
    deduped = _dedupe_reply_targets(targets)
    candidate_ids = _reply_message_ids(deduped)
    used_ids = _used_group_reply_target_ids(session, task, group, candidate_ids)
    invalid_ids = remotely_invalid_reply_target_ids(
        session,
        tenant_id=task.tenant_id,
        task_id=task.id,
        group_id=group.id,
        candidate_ids=candidate_ids,
    )
    return _exclude_used_reply_targets(deduped, used_ids | invalid_ids)


def _admitted_continuity_targets(
    session: Session, task: Task, targets: list[dict],
) -> list[dict]:
    claim_ids = {
        str(target.get("conversation_turn_claim_id") or "") for target in targets
    } - {""}
    if not claim_ids:
        return []
    admitted_ids = set(session.scalars(select(
        TaskGroupDailyMessageSlot.continuity_claim_id,
    ).where(
        TaskGroupDailyMessageSlot.task_id == task.id,
        TaskGroupDailyMessageSlot.continuity_claim_id.in_(claim_ids),
        TaskGroupDailyMessageSlot.state.in_(("open", "unknown")),
    )))
    return [
        target for target in targets
        if str(target.get("conversation_turn_claim_id") or "") in admitted_ids
    ]


def _listener_context_reply_targets(rows: list) -> list[dict]:
    targets = []
    for row in reversed(rows):
        remote_id = str(getattr(row, "remote_message_id", "") or "").strip()
        content = str(getattr(row, "content", "") or "").strip()
        if not remote_id.isdigit() or not content or bool(getattr(row, "is_bot", False)):
            continue
        targets.append({
            "message_id": int(remote_id),
            "author": str(getattr(row, "sender_name", "") or "真人用户").strip(),
            "preview": content[:120],
            "source": "listener_context",
        })
    return targets


def _dedupe_reply_targets(targets: list[dict]) -> list[dict]:
    seen: set[int] = set()
    deduped: list[dict] = []
    for target in targets:
        message_id = int(target.get("message_id") or 0)
        if not message_id or message_id in seen:
            continue
        seen.add(message_id)
        deduped.append(target)
    return deduped


def _exclude_used_reply_targets(targets: list[dict], used_ids: set[int]) -> list[dict]:
    if not used_ids:
        return targets
    return [target for target in targets if int(target.get("message_id") or 0) not in used_ids]


def _reply_message_ids(targets: list[dict]) -> set[int]:
    return {message_id for target in targets if (message_id := int(target.get("message_id") or 0))}

def _historical_group_reply_targets(session: Session, task: Task, group: TgGroup, *, limit: int = 20) -> list[dict]:
    rows = successful_own_history_reply_facts(
        session,
        tenant_id=task.tenant_id,
        task_id=task.id,
        group_id=group.id,
        exclude_used_statuses=IN_FLIGHT_REPLY_TARGET_USAGE_STATUSES,
        limit=limit,
    )
    return [
        target
        for action, remote_id in rows
        if (target := _reply_target_from_action(action, group, remote_message_id=remote_id))
    ]


def _used_group_reply_target_ids(session: Session, task: Task, group: TgGroup, candidate_ids: set[int]) -> set[int]:
    if not candidate_ids:
        return set()
    rows = session.scalars(
        select(Action.payload["reply_to_message_id"].as_integer()).where(
            Action.tenant_id == task.tenant_id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status.in_(IN_FLIGHT_REPLY_TARGET_USAGE_STATUSES),
            Action.payload["group_id"].as_integer() == group.id,
            Action.payload["reply_to_message_id"].as_integer().in_(candidate_ids),
        )
    )
    return {int(row) for row in rows if row}


def _payload_int(action: Action, key: str) -> int:
    payload = action.payload if isinstance(action.payload, dict) else {}
    raw = str(payload.get(key) or "").strip()
    return int(raw) if raw.isdigit() else 0


def _reply_target_from_action(
    action: Action,
    group: TgGroup,
    *,
    remote_message_id: str,
) -> dict | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    raw_id = str(remote_message_id or "").strip()
    content = str(payload.get("message_text") or "").strip()
    if not raw_id.isdigit() or not content:
        return None
    return {
        "message_id": int(raw_id),
        "author": str(payload.get("account_role") or group.title or "历史账号").strip(),
        "preview": content[:120],
        "source": "own_history",
    }


def _reply_target_message_id(item: dict) -> int | None:
    target = item.get("reply_target") if isinstance(item, dict) else None
    return int(target.get("message_id")) if isinstance(target, dict) and target.get("message_id") else None


def _reply_target_label(item: dict) -> str:
    message_id = _reply_target_message_id(item)
    return f"回复消息 #{message_id}" if message_id else ""


def _reply_target_text(item: dict, key: str) -> str:
    target = item.get("reply_target") if isinstance(item, dict) else None
    return str(target.get(key) or "") if isinstance(target, dict) else ""


def _reply_target_binding_payload(item: dict) -> dict[str, Any]:
    target = item.get("reply_target") if isinstance(item, dict) else None
    if not isinstance(target, dict):
        return {}
    return {
        "conversation_event_id": str(target.get("conversation_event_id") or ""),
        "conversation_event_revision": int(target.get("conversation_event_revision") or 0),
        "context_turn_id": str(target.get("context_turn_id") or ""),
        "context_turn_revision": int(target.get("context_turn_revision") or 0),
        "interaction_opportunity_id": str(target.get("interaction_opportunity_id") or ""),
        "conversation_turn_claim_id": str(target.get("conversation_turn_claim_id") or ""),
        "response_not_before_at": target.get("response_not_before_at"),
        "freshness_deadline_at": target.get("freshness_deadline_at"),
    }


def _choose_turn_account(available: list, selected: list, index: int, used_account_ids: set[int], allow_repeat: bool):
    candidates = available or selected
    for account in candidates:
        if account.id not in used_account_ids:
            return account
    if not allow_repeat:
        return None
    return candidates[index % len(candidates)] if candidates else None


def _record_ai_pacing_shortfall(task: Task, requested: int, scheduled: int) -> None:
    """窗口内无合法节奏窗口时守恒可见：不压缩追量，记录 typed shortfall。"""
    stats = dict(task.stats or {})
    stats["pacing_schedule_shortfall_count"] = int(stats.get("pacing_schedule_shortfall_count") or 0) + (requested - scheduled)
    stats["pacing_schedule_shortfall"] = {
        "reason_code": "pacing_capacity_shortfall",
        "requested": requested,
        "scheduled": scheduled,
    }
    task.stats = stats
    task.last_error = "当前日截止前无合法节奏窗口可安排本轮 AI 义务，形成 pacing shortfall"


def _record_generic_warmup_shortfall(task: Task, requested: int) -> None:
    stats = dict(task.stats or {})
    stats["content_shortfall_count"] = int(stats.get("content_shortfall_count") or 0) + requested
    stats["content_shortfall"] = {
        "reason_code": "generic_warmup_question_mix_wait",
        "requested": requested,
        "scheduled": 0,
    }
    task.stats = stats
    task.last_error = "generic_warmup_question_mix_wait"


def _turn_slot_keys(task: Task, cycle_index: int, turn_count: int) -> list[str]:
    return [
        f"ai:{task.id}:cycle:{cycle_index}:turn:{ordinal + 1}"
        for ordinal in range(turn_count)
    ]


def _schedule_times_for_plan(
    session: Session,
    task: Task,
    progress: dict[str, object],
    total: int,
    *,
    mode: str,
    deadline_at: datetime | None = None,
    slot_keys: list[str] | None = None,
) -> list[datetime]:
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        # stable slot：cycle 内 turn 序号（effective_due_rank 等价物）作确定性
        # seed 输入；同一 cycle 重规划得到相同 due_at。
        keys = list(slot_keys or [])
        if len(keys) != total:
            keys = [f"ai:{task.id}:#{index}" for index in range(total)]
        return schedule_due_times(
            total,
            task.pacing_config or {},
            start_at=_now(),
            deadline_at=deadline_at,
            timezone_name=task.timezone,
            deadline_is_utc=True,
            seed_id=f"ai:{task.id}",
            slot_keys=keys,
        )
    hard_times = _hard_hourly_schedule(task, progress, total)
    if hard_times:
        return hard_times
    round_config = _round_schedule_config(task.pacing_config or {}, mode)
    times = schedule_times(
        total,
        round_config,
        start_at=_now(),
        preserve_minimum_spacing=True,
    )
    return reserve_task_schedule_times(
        session,
        task,
        "send_message",
        times,
        pacing_config=round_config,
    )


def _limit_context_bound_turns(
    task: Task,
    config: dict,
    *,
    has_context: bool,
    progress: dict[str, object],
    deferred_generation: bool = False,
    preserve_daily_coverage_batch: bool = False,
    turn_count: int,
    planned_times: list[datetime],
) -> tuple[int, list[datetime]]:
    if preserve_daily_coverage_batch:
        _clear_context_bound_limit_stats(task)
        return turn_count, planned_times
    if not _requires_context_bound_window(config, has_context, progress, deferred_generation):
        _clear_context_bound_limit_stats(task)
        return turn_count, planned_times
    window_seconds = _context_bound_schedule_window_seconds(config)
    cutoff = _task_datetime(task, _now()) + timedelta(seconds=window_seconds)
    allowed_count = len([time_item for time_item in planned_times if _task_datetime(task, time_item) <= cutoff])
    limited_count = min(int(turn_count or 0), allowed_count)
    _record_context_bound_limit_stats(task, turn_count, limited_count, window_seconds)
    limited_times = planned_times[:limited_count]
    return limited_count, limited_times


def _limit_context_bound_quality_schedule(
    task: Task,
    config: dict,
    *,
    has_context: bool,
    progress: dict[str, object],
    deferred_generation: bool = False,
    context_bound_reply_only: bool = False,
    quality_items: list[dict[str, str]],
    planned_times: list[datetime],
) -> tuple[list[dict[str, str]], list[datetime]]:
    if context_bound_reply_only and _requires_context_bound_window(
        config, has_context, progress, deferred_generation=False,
    ):
        return _limit_context_bound_reply_items(
            task, config, quality_items=quality_items, planned_times=planned_times,
        )
    if not _requires_context_bound_window(config, has_context, progress, deferred_generation):
        return quality_items, planned_times
    window_seconds = _context_bound_schedule_window_seconds(config)
    cutoff = _task_datetime(task, _now()) + timedelta(seconds=window_seconds)
    allowed_count = len([item for item in planned_times if _task_datetime(task, item) <= cutoff])
    limited_count = min(len(quality_items), allowed_count)
    _record_context_bound_limit_stats(
        task,
        int((task.stats or {}).get("context_bound_requested_turns") or len(quality_items)),
        limited_count,
        window_seconds,
    )
    limited_times = planned_times[:limited_count]
    return quality_items[:limited_count], limited_times


def _limit_context_bound_reply_items(
    task: Task,
    config: dict,
    *,
    quality_items: list[dict[str, str]],
    planned_times: list[datetime],
) -> tuple[list[dict[str, str]], list[datetime]]:
    window_seconds = _context_bound_schedule_window_seconds(config)
    cutoff = _task_datetime(task, _now()) + timedelta(seconds=window_seconds)
    retained = [
        (item, planned_at)
        for item, planned_at in zip(quality_items, planned_times, strict=False)
        if not item.get("reply_target") or _task_datetime(task, planned_at) <= cutoff
    ]
    requested = sum(1 for item in quality_items if item.get("reply_target"))
    planned = sum(1 for item, _planned_at in retained if item.get("reply_target"))
    if requested != planned:
        _record_context_bound_limit_stats(task, requested, planned, window_seconds)
    if not retained:
        return [], []
    items, times = zip(*retained, strict=True)
    return list(items), list(times)


def _requires_context_bound_window(
    config: dict,
    has_context: bool,
    progress: dict[str, object],
    deferred_generation: bool = False,
) -> bool:
    return (
        bool(has_context)
        and not progress
        and not deferred_generation
        and int(config.get("context_expire_after_messages") or 0) > 0
    )


def _context_bound_schedule_window_seconds(config: dict) -> int:
    try:
        value = int(config.get("context_bound_schedule_window_seconds") or DEFAULT_CONTEXT_BOUND_SCHEDULE_WINDOW_SECONDS)
    except (TypeError, ValueError):
        value = DEFAULT_CONTEXT_BOUND_SCHEDULE_WINDOW_SECONDS
    return max(MIN_CONTEXT_BOUND_SCHEDULE_WINDOW_SECONDS, value)


def _record_context_bound_limit_stats(task: Task, requested: int, planned: int, window_seconds: int) -> None:
    stats = dict(task.stats or {})
    stats["context_bound_requested_turns"] = int(requested or 0)
    stats["context_bound_planned_turns"] = int(planned or 0)
    stats["context_bound_schedule_window_seconds"] = int(window_seconds or 0)
    task.stats = stats


def _clear_context_bound_limit_stats(task: Task) -> None:
    stats = dict(task.stats or {})
    for key in ("context_bound_requested_turns", "context_bound_planned_turns", "context_bound_schedule_window_seconds"):
        stats.pop(key, None)
    task.stats = stats


def _round_schedule_times(total: int, pacing_config: dict, mode: str) -> list[datetime]:
    return schedule_times(
        total,
        _round_schedule_config(pacing_config, mode),
        start_at=_now(),
        preserve_minimum_spacing=True,
    )


def _round_schedule_config(pacing_config: dict, mode: str) -> dict:
    if not (pacing_config or {}).get("operation_profile"):
        return pacing_config or {}
    lo, hi = AI_CHAT_ROUND_INTERVALS_SECONDS.get(mode, AI_CHAT_ROUND_INTERVALS_SECONDS["正常期"])
    hourly_cap = int((pacing_config or {}).get("max_actions_per_hour") or 0)
    if hourly_cap > 0:
        min_gap = max(1, (3600 + hourly_cap - 1) // hourly_cap)
        lo = max(lo, min_gap)
        hi = max(hi, lo)
    return {"mode": "fixed", "interval_seconds_min": lo, "interval_seconds_max": hi, "jitter_percent": 20}


def _hard_hourly_round_config(config: dict, progress: dict[str, object]) -> dict:
    if not progress:
        return config
    updated = dict(config)
    updated["messages_per_round_mode"] = "manual"
    updated["messages_per_round"] = _hard_hourly_batch_size(config, progress)
    updated["allow_account_repeat"] = True
    updated["hard_hourly_planning"] = True
    return updated


def _hard_hourly_batch_size(config: dict, progress: dict[str, object]) -> int:
    deficit = max(1, int(progress.get("deficit") or 1))
    return min(deficit, hard_hourly_planning_rate(progress))


def _hard_hourly_schedule(task: Task, progress: dict[str, object], total: int) -> list[datetime]:
    if not progress:
        return []
    hourly_goal = max(total, hard_hourly_planning_rate(progress))
    return hard_schedule_times(
        total,
        task,
        _now(),
        target_total=hourly_goal,
    )


def _hard_blocker_inc(blockers: dict[str, int], reason: str, progress: dict[str, object]) -> None:
    if not progress:
        return
    blockers[reason] = int(blockers.get(reason) or 0) + 1


def _mark_hard_blocked(task: Task, progress: dict[str, object], reason: str) -> None:
    mark_plan_result(task, progress, 0, {reason: max(1, int(progress.get("deficit") or 1))})


def _mark_waiting_context(
    task: Task,
    config: dict,
    mode: str | None = None,
    ramp_ratio: float | None = None,
    *,
    context_mode: str,
    next_run_at: datetime | None = None,
) -> None:
    resolved_mode, resolved_ratio = (mode, ramp_ratio) if mode and ramp_ratio is not None else ai_cycle_mode(config, task.scheduled_start)
    stats = dict(task.stats or {})
    stats["current_mode"] = resolved_mode
    stats["ramp_ratio"] = resolved_ratio
    stats["context_mode"] = context_mode
    stats["chat_mode"] = "waiting_new_context"
    stats.pop("skip_reason", None)
    stats.pop("duplicate_risk", None)
    stats.pop("hallucination_risk", None)
    if next_run_at:
        normalized_next_run_at = _task_datetime(task, next_run_at)
        stats["idle_continuation_next_run_at"] = normalized_next_run_at.isoformat()
        task.next_run_at = normalized_next_run_at
        task.last_error = WAITING_IDLE_CONTINUATION_MESSAGE
    else:
        stats.pop("idle_continuation_next_run_at", None)
        task.last_error = WAITING_NEW_CONTEXT_MESSAGE
    task.stats = stats


def _should_wait_for_human_context(session: Session, task: Task, usable_context_rows: list, unprocessed_rows: list) -> bool:
    return (bool(usable_context_rows) and not unprocessed_rows) or (not usable_context_rows and _has_generated_before(session, task))


def _has_generated_before(session: Session, task: Task) -> bool:
    return bool(session.scalar(select(Action.id).where(Action.task_id == task.id, Action.action_type == "send_message").limit(1)))


def _idle_continuation_decision(session: Session, task: Task, config: dict) -> dict[str, datetime | bool | None]:
    if config.get("idle_continuation_enabled") is False:
        return {"due": False, "next_run_at": None}
    last_success_at = _last_successful_ai_action_at(session, task)
    if not last_success_at:
        return {"due": False, "next_run_at": None}
    next_run_at = _task_datetime(task, last_success_at) + timedelta(seconds=_idle_continuation_seconds(config))
    return {"due": _task_datetime(task, _now()) >= next_run_at, "next_run_at": next_run_at}


def _idle_continuation_seconds(config: dict) -> int:
    try:
        value = int(config.get("idle_continuation_seconds") or DEFAULT_IDLE_CONTINUATION_SECONDS)
    except (TypeError, ValueError):
        value = DEFAULT_IDLE_CONTINUATION_SECONDS
    return max(30, value)


def _semantic_repeat_window(config: dict) -> int:
    try:
        value = int(config.get("semantic_repeat_window") or 10)
    except (TypeError, ValueError):
        value = 10
    return max(1, min(100, value))


def _last_successful_ai_action_at(session: Session, task: Task) -> datetime | None:
    action = session.scalar(
        select(Action)
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status == "success",
        )
        .order_by(Action.executed_at.desc().nullslast(), Action.scheduled_at.desc(), Action.created_at.desc())
        .limit(1)
    )
    if not action:
        return None
    return _task_datetime(task, action.executed_at or action.scheduled_at or action.created_at)


def _select_cycle_accounts(
    accounts: list,
    config: dict,
    mode: str,
    ramp_ratio: float,
    *,
    has_context: bool,
    cycle_index: int = 1,
    pacing_config: dict | None = None,
    daily_coverage_uncovered_count: int = 0,
    bounded_daily_coverage_batch: bool = False,
) -> tuple[list, int]:
    coverage_count = max(0, min(int(daily_coverage_uncovered_count or 0), len(accounts)))
    if _all_accounts_daily_coverage(config) and coverage_count == 0 and not bool(config.get("hard_hourly_planning")):
        return [], 0
    if bounded_daily_coverage_batch and coverage_count:
        selected = accounts[:coverage_count]
        return selected, coverage_count
    rotated_accounts = accounts if coverage_count else _rotate_accounts(accounts, cycle_index)
    if str(config.get("messages_per_round_mode") or "auto") == "manual":
        messages_per_round = _manual_messages_per_round(config, mode)
        desired = _desired_participant_count(rotated_accounts, config, mode, ramp_ratio)
        turn_count = _manual_turn_count(desired, messages_per_round)
        participant_count = _manual_participant_count(desired, turn_count, len(rotated_accounts), config)
        selected = rotated_accounts[:participant_count]
        if not bool(config.get("allow_account_repeat", True)):
            turn_count = min(turn_count, len(selected))
        return selected, max(1, turn_count)
    turn_count = _auto_messages_per_round(config, mode, has_context, pacing_config or {})
    desired = _desired_participant_count(rotated_accounts, config, mode, ramp_ratio)
    selected_count = min(max(turn_count, desired), len(rotated_accounts))
    selected = rotated_accounts[:selected_count]
    if not bool(config.get("allow_account_repeat", True)):
        turn_count = min(turn_count, len(selected))
    return selected, max(1, turn_count)


def _auto_messages_per_round(config: dict, mode: str, has_context: bool, pacing_config: dict) -> int:
    hourly_cap = int((pacing_config or {}).get("max_actions_per_hour") or 0)
    if hourly_cap > 0:
        rounds = current_hour_rounds(pacing_config or {}, _now())
        base = max(1, (hourly_cap + max(1, rounds) - 1) // max(1, rounds))
    else:
        base = 2 if mode == "静默期" else 5
    if mode == "静默期":
        base = min(base, int(config.get("silent_messages_per_round") or 1))
    if not has_context:
        base = min(base, 3)
    return max(1, base)


def _manual_messages_per_round(config: dict, mode: str) -> int:
    messages_per_round = int(config.get("messages_per_round") or 1)
    if mode == "静默期" and not _all_accounts_daily_coverage(config):
        messages_per_round = min(messages_per_round, int(config.get("silent_messages_per_round") or 1))
    return max(1, messages_per_round)


def _manual_turn_count(desired: int, messages_per_round: int) -> int:
    if messages_per_round == 1:
        return max(1, desired)
    return max(1, messages_per_round)


def _desired_participant_count(accounts: list, config: dict, mode: str, ramp_ratio: float) -> int:
    jitter = float(config.get("participation_jitter") or 0)
    rate = float(config.get("participation_rate") or 0.6)
    desired = max(1, round(len(accounts) * rate * random.uniform(max(0.1, 1 - jitter), 1 + jitter)))
    if mode == "静默期":
        desired = min(desired, int(config.get("silent_max_accounts") or 5))
    return min(desired, len(accounts))


def _manual_participant_count(desired: int, turn_count: int, account_count: int, config: dict) -> int:
    if account_count <= 0:
        return 0
    spread_count = min(turn_count, account_count)
    participant_count = max(desired, spread_count)
    if not bool(config.get("allow_account_repeat", True)):
        participant_count = max(participant_count, spread_count)
    return min(participant_count, account_count)


def _rotate_accounts(accounts: list, cycle_index: int) -> list:
    if len(accounts) <= 1:
        return accounts
    offset = (max(1, int(cycle_index or 1)) - 1) % len(accounts)
    return accounts[offset:] + accounts[:offset]


def _prioritize_account_memory(accounts: list, account_memories: dict[str, str]) -> list:
    if len(accounts) <= 1 or not account_memories:
        return accounts
    return sorted(accounts, key=lambda account: 0 if account_memories.get(str(account.id)) else 1)


def _prioritize_accounts_for_plan(
    accounts: list,
    account_memories: dict[str, str],
    coverage_counts: dict[int, int],
    config: dict,
) -> list:
    if len(accounts) <= 1:
        return accounts
    if _hard_hourly_planning(config):
        return accounts
    if not _all_accounts_daily_coverage(config):
        return _prioritize_account_memory(accounts, account_memories)
    target = _coverage_target_per_account(config)
    return sorted(
        accounts,
        key=lambda account: (
            0 if max(0, target - int(coverage_counts.get(int(account.id), 0))) > 0 else 1,
            int(coverage_counts.get(int(account.id), 0)),
            0 if account_memories.get(str(account.id)) else 1,
        ),
    )


def _hard_hourly_planning(config: dict) -> bool:
    return bool(config.get("hard_hourly_planning"))


def _with_active_conversation_targets(session: Session, task: Task, config: dict, group: TgGroup) -> dict:
    usage = _recent_conversation_target_usage(session, task, group)
    topic = _choose_topic_direction(config, group, usage.get("topics", {}))
    teacher = _choose_teacher_target(config, usage.get("teachers", {}))
    return {
        **config,
        "active_topic_direction": topic,
        "active_teacher_target": teacher,
        "conversation_target_usage": usage,
    }


def _choose_topic_direction(config: dict, group: TgGroup, recent_counts: dict[str, int] | None = None) -> dict:
    directions = [item for item in config.get("topic_directions") or [] if str(item.get("title") or "").strip()]
    if directions:
        directions = _least_recently_used_items(directions, recent_counts or {}, label_key="title")
        total = sum(max(0.01, float(item.get("weight") or 1)) for item in directions)
        marker = random.random() * total
        cursor = 0.0
        for item in directions:
            cursor += max(0.01, float(item.get("weight") or 1))
            if marker <= cursor:
                return dict(item)
    fallback = str(group.topic_direction or "同城老客交流与避坑讨论").strip()
    if fallback in {"日常讨论、活动答疑", "群聊日常活跃"}:
        fallback = "同城老客交流与避坑讨论"
    return {"title": fallback, "description": "", "weight": 1}


def _choose_teacher_target(config: dict, recent_counts: dict[str, int] | None = None) -> dict:
    teachers = [item for item in config.get("teacher_targets") or [] if str(item.get("name") or "").strip()]
    if not teachers:
        return {}
    teachers = _least_recently_used_items(teachers, recent_counts or {}, label_key="name")
    return dict(sorted(teachers, key=lambda item: int(item.get("priority") or 1), reverse=True)[0])


def _least_recently_used_items(items: list[dict], recent_counts: dict[str, int], *, label_key: str) -> list[dict]:
    if len(items) <= 1:
        return items
    usage_counts = [_usage_count(item, recent_counts, label_key) for item in items]
    least_used = min(usage_counts)
    return [item for item, count in zip(items, usage_counts) if count == least_used]


def _recent_conversation_target_usage(session: Session, task: Task, group: TgGroup) -> dict[str, dict[str, int]]:
    usage = {"topics": {}, "teachers": {}}
    memory_rows = session.scalars(
        select(AiGroupMessageMemory)
        .where(
            AiGroupMessageMemory.tenant_id == task.tenant_id,
            AiGroupMessageMemory.group_id == group.id,
            AiGroupMessageMemory.status.in_(RECENT_TARGET_USAGE_MEMORY_STATUSES),
        )
        .order_by(AiGroupMessageMemory.planned_at.desc())
        .limit(RECENT_TARGET_USAGE_SCAN_LIMIT)
    )
    for memory in memory_rows:
        _increment_usage(usage["topics"], _normalize_conversation_label(memory.topic_direction))
        _increment_usage(usage["teachers"], _normalize_conversation_label(memory.teacher_target))
    if _has_conversation_usage(usage):
        return usage
    rows = session.scalars(
        select(Action)
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status.in_(RECENT_TARGET_USAGE_STATUSES),
        )
        .order_by(Action.created_at.desc())
        .limit(RECENT_TARGET_USAGE_SCAN_LIMIT)
    )
    for action in rows:
        payload = action.payload if isinstance(action.payload, dict) else {}
        if _payload_group_id(payload) != group.id:
            continue
        _increment_usage(usage["topics"], _conversation_label(payload.get("topic_direction"), "title"))
        _increment_usage(usage["teachers"], _conversation_label(payload.get("teacher_target"), "name"))
    return usage


def _has_conversation_usage(usage: dict[str, dict[str, int]]) -> bool:
    return bool(usage.get("topics") or usage.get("teachers"))


def _usage_count(item: dict, recent_counts: dict[str, int], label_key: str) -> int:
    return int(recent_counts.get(_normalize_conversation_label(str(item.get(label_key) or "")), 0) or 0)


def _increment_usage(container: dict[str, int], label: str) -> None:
    if label:
        container[label] = int(container.get(label, 0) or 0) + 1


def _conversation_label(value: object, key: str) -> str:
    if isinstance(value, dict):
        return _normalize_conversation_label(str(value.get(key) or ""))
    return _normalize_conversation_label(str(value or ""))


def _normalize_conversation_label(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def _payload_group_id(payload: dict) -> int:
    raw = str(payload.get("group_id") or "").strip()
    return int(raw) if raw.isdigit() else 0


def _conversation_target_usage_config(config: dict) -> dict[str, dict[str, int]]:
    usage = config.get("conversation_target_usage") if isinstance(config.get("conversation_target_usage"), dict) else {}
    return {
        "topics": dict(usage.get("topics") or {}) if isinstance(usage.get("topics"), dict) else {},
        "teachers": dict(usage.get("teachers") or {}) if isinstance(usage.get("teachers"), dict) else {},
    }


def _slot_topic_directions(config: dict) -> list[dict]:
    directions = [
        dict(item)
        for item in config.get("topic_directions") or []
        if str(item.get("title") or "").strip()
    ]
    if directions:
        return directions
    active = config.get("active_topic_direction") if isinstance(config.get("active_topic_direction"), dict) else {}
    return [dict(active)] if str(active.get("title") or "").strip() else []


def _slot_teacher_targets(config: dict) -> list[dict]:
    teachers = [
        dict(item)
        for item in config.get("teacher_targets") or []
        if str(item.get("name") or "").strip()
    ]
    if teachers:
        return teachers
    active = config.get("active_teacher_target") if isinstance(config.get("active_teacher_target"), dict) else {}
    return [dict(active)] if str(active.get("name") or "").strip() else []


def _quality_topic_direction(quality_item: dict, config: dict) -> dict:
    frozen = _quality_slot(quality_item)
    slot_topic = frozen.get("topic_direction")
    if isinstance(slot_topic, dict) and str(slot_topic.get("title") or "").strip():
        return dict(slot_topic)
    if frozen.get("topic_mode"):
        return {}
    active = config.get("active_topic_direction") if isinstance(config.get("active_topic_direction"), dict) else {}
    return dict(active) if active else {}


def _quality_teacher_target(quality_item: dict, config: dict) -> dict:
    slot_teacher = _quality_slot(quality_item).get("teacher_target")
    if isinstance(slot_teacher, dict) and str(slot_teacher.get("name") or "").strip():
        return dict(slot_teacher)
    active = config.get("active_teacher_target") if isinstance(config.get("active_teacher_target"), dict) else {}
    return dict(active) if active else {}


def _active_topic_text(config: dict, group: TgGroup) -> str:
    if config.get("topic_participation_rate") is not None:
        return ""
    topic = config.get("active_topic_direction") or _choose_topic_direction(config, group)
    return _topic_target_text(topic, group)


def _topic_target_text(topic: dict, group: TgGroup) -> str:
    title = str(topic.get("title") or "").strip()
    description = str(topic.get("description") or "").strip()
    if not title:
        title = str(group.topic_direction or "群聊日常活跃").strip()
    return f"{title}：{description}" if title and description else title


def _active_teacher_text(config: dict) -> str:
    teacher = config.get("active_teacher_target") or {}
    return _teacher_target_text(teacher)


def _teacher_target_text(teacher: dict) -> str:
    name = str(teacher.get("name") or "").strip()
    description = str(teacher.get("description") or "").strip()
    return f"{name}：{description}" if name and description else name


def _consecutive_burst_plan(config: dict, count: int, allow_repeat: bool, cycle_id: str) -> dict[int, dict]:
    # Humanization PRD: same-account consecutive burst is removed.
    return {}


def _bootstrap_history(config: dict, group: TgGroup) -> str:
    topic = _active_topic_text(config, group)
    if not topic:
        topic = "围绕群内日常交流自然开场，轻松抛出一个大家容易接上的话题"
    teacher = _active_teacher_text(config)
    suffix = f"，讨论老师参考“{teacher}”" if teacher else ""
    return f"当前群暂无可用历史消息。请以“{topic}”为方向{suffix}，生成自然开场，不要提到系统、任务或 AI。"


def _idle_continuation_history(config: dict, group: TgGroup, previous_ai_messages: list[str]) -> str:
    topic = _active_topic_text(config, group) or "群聊日常活跃"
    teacher = _active_teacher_text(config)
    recent_ai = " / ".join(_clean_topic_text(text) for text in previous_ai_messages[-3:])
    recent_ai = recent_ai.strip(" /")
    parts = [
        f"群内暂时没有新的真人消息。请围绕“{topic}”补一句具体小事，像群友随手回消息。",
        "必须避免重复上一轮表达，不要提到系统、任务或 AI。",
    ]
    if teacher:
        parts.append(f"讨论老师参考：{teacher}。")
    if recent_ai:
        parts.append(f"上一轮 AI 已说：{recent_ai}。请避开原句，只接一个轻量问题或泛化观察，不要编具体经历、到场感受、位置或回访。")
    return "\n".join(parts)


def _context_mode(context_rows: list, idle_continuation: bool) -> str:
    if idle_continuation:
        return "idle_continuation"
    return "history" if context_rows else "bootstrap"


def _generation_source(context_rows: list, idle_continuation: bool) -> str:
    if idle_continuation:
        return "idle_continuation"
    return "human_context" if context_rows else "bootstrap"


def _chat_mode(context_rows: list, idle_continuation: bool) -> str:
    if idle_continuation:
        return CHAT_MODE_IDLE_WARMUP
    return CHAT_MODE_REPLY if context_rows else CHAT_MODE_BOOTSTRAP


def _mark_quality_skip(
    task: Task,
    config: dict,
    mode: str,
    ramp_ratio: float,
    context_mode: str,
    chat_mode: str,
    quality_stats: dict[str, str],
) -> None:
    stats = dict(task.stats or {})
    stats["current_mode"] = mode
    stats["ramp_ratio"] = ramp_ratio
    stats["context_mode"] = context_mode
    stats["chat_mode"] = chat_mode
    stats["skip_reason"] = quality_stats.get("skip_reason") or "quality_gate"
    if quality_stats.get("duplicate_risk"):
        stats["duplicate_risk"] = quality_stats["duplicate_risk"]
    if quality_stats.get("hallucination_risk"):
        stats["hallucination_risk"] = quality_stats["hallucination_risk"]
    task.stats = stats
    if quality_stats.get("skip_reason") == "hallucination_risk":
        task.last_error = AI_QUALITY_ANCHOR_SKIP_MESSAGE
    elif quality_stats.get("skip_reason") == "duplicate_risk":
        task.last_error = AI_QUALITY_DUPLICATE_SKIP_MESSAGE
    else:
        task.last_error = AI_GENERATION_UNAVAILABLE_MESSAGE


def _topic_thread_summary(config: dict, group: TgGroup, context_rows: list, previous_ai_messages: list[str]) -> str:
    parts: list[str] = []
    topic = _active_topic_text(config, group)
    if topic:
        parts.append(f"主线方向：{topic[:80]}")
    teacher = _active_teacher_text(config)
    if teacher:
        parts.append(f"讨论老师：{teacher[:80]}")
    recent_human = [_clean_topic_text(getattr(row, "content", "")) for row in context_rows[-3:]]
    recent_human = [text for text in recent_human if text]
    if recent_human:
        parts.append("最近真人上下文：" + " / ".join(recent_human))
    recent_ai = [_clean_topic_text(text) for text in previous_ai_messages[-3:]]
    recent_ai = [text for text in recent_ai if text]
    if recent_ai:
        parts.append("上一轮 AI 已说：" + " / ".join(recent_ai))
    if not parts:
        return ""
    return "；".join(parts)[:500]


def _topic_plan_summary(config: dict, group: TgGroup, topic_thread: str, turn_count: int) -> str:
    topic = _active_topic_text(config, group) or "当前真人上下文或群内日常交流"
    teacher = _active_teacher_text(config)
    anchors = [part.strip() for part in re.split(r"[；/]", topic_thread or "") if part.strip()]
    anchor = anchors[-1] if anchors else f"主线方向：{topic[:80]}"
    teacher_hint = f"，讨论老师参考“{teacher[:40]}”" if teacher else ""
    steps = [
        f"1. 贴近现场：从“{anchor[:80]}”里挑一个最像真人会接的点{teacher_hint}，短句承接。",
        f"2. 补充一点生活化细节：只使用上下文已有的、和“{topic[:60]}”相关的小信息，不像科普。",
        "3. 轻轻问一句：问题要小、具体、容易回，不要问“大家怎么看”。",
        "4. 收到一个具体细节上：把内容放回上一条真人上下文，别总结成公告。",
        "5. 换个小细节：如果前面已经有人接话，就从具体反应、克制分歧或继续求证切入。",
    ]
    return "\n".join(steps[: max(1, min(int(turn_count or 1), len(steps)))])


def _clean_topic_text(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if _looks_like_internal_prompt(text) or _looks_like_generated_noise(text):
        return ""
    return text[:80]


def ai_cycle_mode(config: dict, scheduled_start: datetime | None = None, now: datetime | None = None) -> tuple[str, float]:
    current = now or _now()
    mode, ratio, _intensity = operation_intensity(config.get("pacing_config") or config, current)
    if (config.get("pacing_config") or {}).get("operation_profile") or config.get("operation_profile"):
        return mode, round(ratio, 3)
    mode = "正常期"
    if config.get("silent_mode_enabled", True) and _in_time_window(current.time(), str(config.get("silent_start") or "23:00"), str(config.get("silent_end") or "08:00")):
        mode = "静默期"
    ramp_minutes = int(config.get("ramp_up_minutes") or 0)
    if ramp_minutes <= 0:
        return mode, 1.0
    current_in_zone = to_zone(current)
    start = scheduled_start or current_in_zone.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_minutes = max(0.0, (current_in_zone - to_zone(start)).total_seconds() / 60)
    if elapsed_minutes >= ramp_minutes:
        return mode, 1.0
    start_ratio = float(config.get("ramp_start_ratio") or 0.3)
    ratio = min(1.0, max(start_ratio, start_ratio + (1 - start_ratio) * (elapsed_minutes / max(ramp_minutes, 1))))
    return ("启动期" if mode == "正常期" else mode), round(ratio, 3)


def _in_time_window(current: time, start_raw: str, end_raw: str) -> bool:
    start = _parse_time(start_raw, time(23, 0))
    end = _parse_time(end_raw, time(8, 0))
    return start <= current < end if start < end else current >= start or current < end


def _parse_time(value: str, fallback: time) -> time:
    try:
        hour, minute = [int(part) for part in value.split(":", 1)]
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError):
        return fallback


def _task_datetime(task: Task, value: datetime) -> datetime:
    return to_zone(value, parse_zone(task.timezone))


def _context_fingerprint(row) -> str:
    return f"context:{row.id}:{row.remote_message_id}"


def _recent_ai_messages(session: Session, task: Task, *, limit: int) -> list[str]:
    messages: list[str] = []
    rows = session.scalars(
        select(Action)
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status == "success",
        )
        .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
        .limit(max(1, int(limit)))
    )
    for action in rows:
        payload = action.payload if isinstance(action.payload, dict) else {}
        content = str(payload.get("message_text") or "").strip()
        if content and not _looks_like_internal_prompt(content):
            messages.append(content)
    return list(reversed(messages))


def _recent_planned_ai_messages(session: Session, task: Task, *, limit: int) -> list[str]:
    messages: list[str] = []
    rows = session.scalars(
        select(Action)
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status.in_(RECENT_PLANNED_AI_STATUSES),
        )
        .order_by(Action.created_at.desc())
        .limit(max(1, int(limit)))
    )
    for action in rows:
        payload = action.payload if isinstance(action.payload, dict) else {}
        content = str(payload.get("message_text") or "").strip()
        if content and not _looks_like_internal_prompt(content):
            messages.append(content)
    return list(reversed(messages))


def _recent_group_memory_messages(session: Session, task: Task, group: TgGroup, *, limit: int) -> list[str]:
    rows = session.scalars(
        select(AiGroupMessageMemory)
        .where(
            AiGroupMessageMemory.tenant_id == task.tenant_id,
            AiGroupMessageMemory.group_id == group.id,
            AiGroupMessageMemory.status.in_(RECENT_TARGET_USAGE_MEMORY_STATUSES),
        )
        .order_by(AiGroupMessageMemory.planned_at.desc())
        .limit(max(1, int(limit)))
    )
    messages = [str(row.normalized_text or row.raw_text or "").strip() for row in rows]
    return list(reversed([message for message in messages if message and not _looks_like_internal_prompt(message)]))


def _expire_open_profileless_actions(session: Session, task: Task, active_profile_account_ids) -> int:
    account_ids = [int(account_id) for account_id in active_profile_account_ids if int(account_id or 0) > 0]
    if not account_ids:
        return 0
    actions = list(
        session.scalars(
            select(Action).where(
                Action.task_id == task.id,
                Action.task_type == "group_ai_chat",
                Action.action_type == "send_message",
                Action.status.in_(VOICE_PROFILE_REPLAN_OPEN_STATUSES),
                Action.account_id.in_(account_ids),
            ).with_for_update(skip_locked=True, of=Action)
        )
    )
    expired = 0
    current_time = _now()
    for action in actions:
        payload = action.payload if isinstance(action.payload, dict) else {}
        if _payload_voice_profile_version(payload) > 0:
            continue
        _skip_profileless_action_for_replan(session, action, current_time)
        expired += 1
    if expired:
        stats = dict(task.stats or {})
        stats["voice_profile_replanned_open_action_count"] = int(stats.get("voice_profile_replanned_open_action_count") or 0) + expired
        task.stats = stats
    return expired


def _skip_skewed_hard_hourly_open_actions_for_replan(session: Session, task: Task, selected_account_count: int) -> int:
    actions = _open_hard_hourly_actions_for_distribution_replan(session, task)
    skew = _hard_hourly_distribution_skew([int(action.account_id or 0) for action in actions], selected_account_count)
    if not skew:
        return 0
    current_time = _now()
    for action in actions:
        _skip_distribution_skew_action_for_replan(session, action, current_time)
    stats = dict(task.stats or {})
    stats["hard_hourly_distribution_replanned_open_action_count"] = int(
        stats.get("hard_hourly_distribution_replanned_open_action_count") or 0
    ) + len(actions)
    stats["hard_hourly_distribution_skew"] = skew
    task.stats = stats
    return len(actions)


def _skip_legacy_hard_hourly_open_actions_for_daily_coverage_replan(
    session: Session,
    task: Task,
    config: dict,
) -> int:
    if not _daily_coverage_enforced(config):
        return 0
    current_time = _now()
    skipped = 0
    for action in _open_hard_hourly_actions_for_distribution_replan(session, task):
        payload = action.payload if isinstance(action.payload, dict) else {}
        if str(payload.get("coverage_ledger_id") or "").strip():
            continue
        _skip_open_action_for_replan(
            session,
            action,
            current_time,
            error_code=ALL_ACCOUNT_DAILY_COVERAGE_REPLAN_CODE,
            message=ALL_ACCOUNT_DAILY_COVERAGE_REPLAN_MESSAGE,
        )
        skipped += 1
    if skipped:
        stats = dict(task.stats or {})
        stats["all_account_daily_coverage_replanned_open_action_count"] = int(
            stats.get("all_account_daily_coverage_replanned_open_action_count") or 0
        ) + skipped
        task.stats = stats
    return skipped


def _open_hard_hourly_actions_for_distribution_replan(session: Session, task: Task) -> list[Action]:
    rows = session.scalars(
        select(Action)
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status.in_(VOICE_PROFILE_REPLAN_OPEN_STATUSES),
            func.coalesce(
                Action.payload["hard_hourly_target"].as_boolean(), False,
            ).is_(True),
            Action.account_id.is_not(None),
            Action.account_id > 0,
        )
        .order_by(Action.scheduled_at.asc().nullslast(), Action.id.asc())
        .with_for_update(skip_locked=True, of=Action)
    )
    return list(rows)


def _action_is_hard_hourly_target(action: Action) -> bool:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return bool(payload.get("hard_hourly_target")) and int(action.account_id or 0) > 0


def _skip_distribution_skew_action_for_replan(session: Session, action: Action, current_time: datetime) -> None:
    _skip_open_action_for_replan(
        session,
        action,
        current_time,
        error_code="hard_hourly_distribution_skew_replan",
        message="硬目标账号分布偏斜，旧规划已跳过等待重新生成",
    )


def _payload_voice_profile_version(payload: dict) -> int:
    return max(int(payload.get("account_voice_profile_version") or 0), int(payload.get("account_mask_version") or 0))


def _skip_profileless_action_for_replan(session: Session, action: Action, current_time: datetime) -> None:
    _skip_open_action_for_replan(
        session,
        action,
        current_time,
        error_code="voice_profile_replan",
        message="账号面具已生效，旧规划已跳过等待重新生成",
    )


def _skip_open_action_for_replan(
    session: Session,
    action: Action,
    current_time: datetime,
    *,
    error_code: str,
    message: str,
) -> None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    action.status = "skipped"
    action.executed_at = current_time
    action.lease_owner = ""
    action.lease_expires_at = None
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.result = {"error_code": error_code, "message": message}
    if memory_id := str(payload.get("ai_message_memory_id") or "").strip():
        mark_group_ai_message_result(
            session,
            memory_id,
            status="expired_before_send",
            result={"error_code": error_code, "action_id": action.id},
        )


def _recent_account_memories(
    session: Session,
    task: Task,
    account_ids: list[int],
    *,
    group_id: int,
    depth: int,
) -> dict[str, str]:
    if depth <= 0 or not account_ids:
        return {}
    wanted = set(account_ids)
    memories: dict[int, list[str]] = {account_id: [] for account_id in wanted}
    rows = session.scalars(
        select(Action)
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status == "success",
            Action.account_id.in_(wanted),
            Action.payload["group_id"].as_integer() == group_id,
        )
        .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
        .limit(max(len(wanted) * depth * 2, depth))
    )
    for action in rows:
        if action.account_id not in wanted or len(memories[action.account_id]) >= depth:
            continue
        _append_account_memory(memories, action, depth=depth)
    return {str(account_id): "；".join(reversed(items)) for account_id, items in memories.items() if items}


def _append_account_memory(memories: dict[int, list[str]], action: Action, *, depth: int, source_label: str = "") -> None:
    if action.account_id not in memories or len(memories[action.account_id]) >= depth:
        return
    payload = action.payload if isinstance(action.payload, dict) else {}
    content = str(payload.get("message_text") or "").strip()
    if not content or _looks_like_internal_prompt(content):
        return
    role = str(payload.get("account_role") or "").strip()
    intent = str(payload.get("intent") or "").strip()
    label = " / ".join(part for part in [source_label, role, intent] if part)
    memories[action.account_id].append(f"{label}: {content[:80]}" if label else content[:80])


def account_profile_summaries(
    session: Session,
    task: Task,
    account_ids: list[int],
    *,
    group_id: int,
    recent_limit: int = 5,
) -> dict[str, str]:
    if not account_ids:
        return {}
    wanted = {int(account_id) for account_id in account_ids if account_id}
    if not wanted:
        return {}
    totals = _account_profile_totals(session, task, wanted, group_id=group_id)
    if not totals:
        return {}
    rows = _account_profile_rows(
        session,
        task,
        wanted,
        group_id=group_id,
        recent_limit=recent_limit,
    )
    profiles = _empty_account_profiles(wanted, totals)
    _accumulate_account_profiles(profiles, rows, recent_limit=recent_limit)
    return _render_account_profiles(profiles, totals)


def _account_profile_totals(
    session: Session,
    task: Task,
    wanted: set[int],
    *,
    group_id: int,
) -> dict[int, int]:
    rows = session.execute(
        select(Action.account_id, func.count(Action.id))
        .join(Task, Task.id == Action.task_id)
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status == "success",
            Action.account_id.in_(wanted),
            Task.tenant_id == task.tenant_id,
            Task.type == "group_ai_chat",
            Task.deleted_at.is_(None),
            Action.payload["group_id"].as_integer() == group_id,
        )
        .group_by(Action.account_id)
    )
    return {int(account_id): int(count) for account_id, count in rows}


def _account_profile_rows(
    session: Session,
    task: Task,
    wanted: set[int],
    *,
    group_id: int,
    recent_limit: int,
):
    return list(session.execute(
        select(Action, Task.name)
        .join(Task, Task.id == Action.task_id)
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status == "success",
            Action.account_id.in_(wanted),
            Task.tenant_id == task.tenant_id,
            Task.type == "group_ai_chat",
            Task.deleted_at.is_(None),
            Action.payload["group_id"].as_integer() == group_id,
        )
        .order_by(Action.account_id.asc(), Action.executed_at.desc().nullslast(), Action.created_at.desc())
        .limit(max(len(wanted) * recent_limit * 3, recent_limit))
    ))


def _empty_account_profiles(
    wanted: set[int],
    totals: dict[int, int],
) -> dict[int, dict[str, object]]:
    return {
        account_id: {"roles": {}, "intents": {}, "tasks": set(), "messages": []}
        for account_id in wanted
        if int(totals.get(account_id) or 0) > 0
    }


def _accumulate_account_profiles(
    profiles: dict[int, dict[str, object]],
    rows,
    *,
    recent_limit: int,
) -> None:
    for action, task_name in rows:
        if action.account_id not in profiles:
            continue
        payload = action.payload if isinstance(action.payload, dict) else {}
        item = profiles[action.account_id]
        task_names = item["tasks"]
        if isinstance(task_names, set) and task_name:
            task_names.add(str(task_name))
        _profile_count(item["roles"], str(payload.get("account_role") or "").strip())
        _profile_count(item["intents"], str(payload.get("intent") or "").strip())
        messages = item["messages"]
        content = str(payload.get("message_text") or "").strip()
        if isinstance(messages, list) and content and not _looks_like_internal_prompt(content) and len(messages) < recent_limit:
            messages.append(content[:60])


def _render_account_profiles(
    profiles: dict[int, dict[str, object]],
    totals: dict[int, int],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for account_id, item in profiles.items():
        roles = _top_profile_values(item["roles"])
        intents = _top_profile_values(item["intents"])
        tasks = item["tasks"] if isinstance(item["tasks"], set) else set()
        messages = item["messages"] if isinstance(item["messages"], list) else []
        parts = [
            f"历史成功发言 {int(totals.get(account_id) or 0)} 次",
            f"关联任务 {len(tasks)} 个" if tasks else "",
            f"常用角色：{'、'.join(roles)}" if roles else "",
            f"常见意图：{'、'.join(intents)}" if intents else "",
            f"近期表达：{' / '.join(messages[:2])}" if messages else "",
        ]
        result[str(account_id)] = "；".join(part for part in parts if part)
    return result


def _profile_count(container: object, value: str) -> None:
    if not value or not isinstance(container, dict):
        return
    container[value] = int(container.get(value, 0) or 0) + 1


def _top_profile_values(container: object, *, limit: int = 3) -> list[str]:
    if not isinstance(container, dict):
        return []
    return [
        str(key)
        for key, _count in sorted(container.items(), key=lambda item: (-int(item[1] or 0), str(item[0])))[:limit]
        if str(key).strip()
    ]


def _looks_like_internal_prompt(content: str) -> bool:
    text = content or ""
    markers = (
        "当前群暂无可用历史消息",
        "不要提到系统、任务或 AI",
        "不要提到系统、任务或AI",
        "生成自然开场",
        "刚看到大家提到“刚看到大家提到",
        "[已撤回的内部提示词",
        "刚看到大家提到",
        "刚看到有人聊这个",
        "看大家聊",
        "顺着这个话题说",
        "这个点挺有意思",
        "这个点我也留意到了",
        "可以继续聊聊",
        "有经验的朋友也可以补充",
        "这个话题",
        "自然接一句",
        "换个角度",
        "轻量推进",
        "值得讨论",
    )
    return (
        any(marker in text for marker in markers)
        or looks_like_generated_template_noise(text)
        or looks_like_operator_ui_content(text)
    )


def _looks_like_generated_noise(content: str) -> bool:
    text = content or ""
    if _looks_like_internal_prompt(text):
        return True
    return text.count("“") + text.count("”") >= 4


def _is_usable_context_message(content: str) -> bool:
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    compact = re.sub(r"\s+", "", text)
    if _looks_like_internal_prompt(text):
        return False
    if contains_coarse_language(text):
        return False
    if compact.isdigit():
        return False
    if len(compact) <= 8 and len(set(compact)) <= 2:
        return False
    return True


def _is_human_context_row(row) -> bool:
    return not bool(getattr(row, "is_bot", False))


def _topic_relevant_context_rows(config: dict, rows: list) -> list:
    active_topic = config.get("active_topic_direction") or {}
    topic_parts = [
        str(active_topic.get("title") or ""),
        str(active_topic.get("description") or ""),
        _active_teacher_text(config),
    ]
    topic = " ".join(part.strip() for part in topic_parts if part.strip())
    if not topic or not rows:
        return rows
    keywords = _topic_keywords(topic)
    if not keywords:
        return rows
    matched = [row for row in rows if any(keyword in str(getattr(row, "content", "") or "") for keyword in keywords)]
    return matched or rows


def _topic_keywords(topic: str) -> set[str]:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", " ", topic).strip()
    parts = [part for part in re.split(r"\s+", cleaned) if len(part) >= 2]
    keywords = set(parts)
    for part in parts:
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", part):
            keywords.update(part[index:index + 2] for index in range(0, len(part) - 1))
    return {keyword for keyword in keywords if keyword}


def _drop_repeated_ai_messages(contents: list[str], previous_messages: list[str]) -> list[str]:
    accepted: list[str] = []
    seen_starts: set[str] = set()
    for content in contents:
        normalized = _normalize_for_similarity(content)
        if not normalized:
            continue
        cluster = _semantic_cluster(content)
        start = normalized[:8]
        if start in seen_starts:
            continue
        if any(_is_similarity_duplicate(normalized, cluster, previous, threshold=0.62) for previous in previous_messages):
            continue
        if any(_is_similarity_duplicate(normalized, cluster, existing, threshold=0.68) for existing in accepted):
            continue
        seen_starts.add(start)
        accepted.append(content)
    return accepted


def _is_similarity_duplicate(normalized: str, cluster: str, previous: str, *, threshold: float) -> bool:
    previous_normalized = _normalize_for_similarity(previous)
    if not previous_normalized:
        return False
    if cluster and cluster == _semantic_cluster(previous):
        return True
    return _similarity(normalized, previous_normalized) >= threshold


def _quality_filter_ai_messages(
    contents: list[str],
    previous_messages: list[str],
    *,
    chat_mode: str,
    anchor_message_ids: list[int],
    fact_anchor_required: bool,
    low_confidence_silence_enabled: bool,
    limit: int,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    accepted: list[dict[str, str]] = []
    accepted_clusters: set[str] = set()
    previous_clusters = {_message_dedupe_key(message) for message in previous_messages}
    previous_clusters.discard("")
    stats: dict[str, object] = {"ai_generation_candidate_count": len(contents)}
    for content in contents:
        cluster = _semantic_cluster(content)
        dedupe_key = _message_dedupe_key(content)
        item = {
            "content": content,
            "semantic_cluster": cluster,
            "duplicate_risk": "",
            "hallucination_risk": "",
            "quality_skip_reason": "",
        }
        if _looks_like_vague_ai_filler(content):
            _record_quality_rejection(stats, "template_shell_limited", content, detail="vague_ai_filler")
            stats["skip_reason"] = stats.get("skip_reason") or "template_shell_limited"
            continue
        if dedupe_key and (dedupe_key in accepted_clusters or dedupe_key in previous_clusters):
            _record_quality_rejection(stats, "duplicate_message", content, detail="semantic_cluster")
            stats["duplicate_risk"] = "semantic_cluster"
            stats["skip_reason"] = stats.get("skip_reason") or "duplicate_risk"
            continue
        if fact_anchor_required and _has_unanchored_idle_fact(content, chat_mode=chat_mode, anchor_message_ids=anchor_message_ids):
            _record_quality_rejection(stats, "hallucination_risk", content, detail="unanchored_idle_fact")
            stats["hallucination_risk"] = "high"
            stats["skip_reason"] = "hallucination_risk"
            continue
        if low_confidence_silence_enabled and chat_mode == CHAT_MODE_BOOTSTRAP and _looks_like_fact_claim(content):
            _record_quality_rejection(stats, "context_insufficient", content, detail="low_confidence_bootstrap")
            stats["hallucination_risk"] = "low_confidence_bootstrap"
            stats["skip_reason"] = "hallucination_risk"
            continue
        accepted.append(item)
        if dedupe_key:
            accepted_clusters.add(dedupe_key)
        if len(accepted) >= max(1, int(limit or 1)):
            break
    return accepted, stats


def _message_dedupe_key(content: str) -> str:
    cluster = _semantic_cluster(content)
    if cluster:
        return cluster
    normalized = _normalize_for_similarity(content)
    return f"exact:{normalized}" if normalized else ""


def _looks_like_vague_ai_filler(content: str) -> bool:
    text = _normalize_for_similarity(content)
    if not text or not any(marker in text for marker in VAGUE_AI_FILLER_MARKERS):
        return False
    if "?" in str(content) or "？" in str(content):
        return False
    return not any(marker in text for marker in VAGUE_AI_FILLER_DETAIL_MARKERS)


def _duplicate_window_quality_reason(duplicate_window: str) -> str:
    return "template_shell_limited" if str(duplicate_window or "").startswith("30d_template_shell") else "duplicate_message"


def _record_quality_rejection(
    stats: dict[str, object],
    reason: str,
    content: str,
    *,
    detail: str = "",
    account_id: int | None = None,
) -> None:
    counts = dict(stats.get("quality_rejection_counts") or {})
    counts[reason] = int(counts.get(reason) or 0) + 1
    stats["quality_rejection_counts"] = counts
    samples = list(stats.get("quality_rejection_samples") or [])
    same_reason_count = sum(1 for item in samples if str(item.get("reason") or "") == reason)
    if same_reason_count >= QUALITY_REJECTION_SAMPLE_LIMIT:
        return
    samples.append({"reason": reason, "content": content, "status": "filtered", "account_id": account_id, "detail": detail})
    stats["quality_rejection_samples"] = samples


def _voice_profile_match_decision_for_item(content: str, voice_profile: dict, quality_item: dict) -> dict[str, object]:
    if quality_item.get("quality_fallback"):
        return {"score": VOICE_PROFILE_MATCH_SCORE, "reason": "安全质量兜底"}
    return _voice_profile_match_decision(content, voice_profile)


def _voice_profile_match_decision(content: str, voice_profile: dict) -> dict[str, object]:
    summary = str(voice_profile.get("summary") or "")
    if not summary.strip():
        return {"score": VOICE_PROFILE_MATCH_SCORE, "reason": ""}
    normalized_summary = _normalize_for_similarity(summary)
    emoji_count = len(EMOJI_PATTERN.findall(content))
    normalized_content = _normalize_for_similarity(content)
    if _profile_rejects_emoji(normalized_summary) and (emoji_count >= 2 or _is_emoji_only_message(content)):
        return {"score": VOICE_PROFILE_MISMATCH_SCORE, "reason": "账号面具要求少表情"}
    if "短句" in normalized_summary and len(normalized_content) > VOICE_PROFILE_LONG_SHORT_SENTENCE_LIMIT:
        return {"score": VOICE_PROFILE_MISMATCH_SCORE, "reason": "账号面具要求短句"}
    return {"score": VOICE_PROFILE_MATCH_SCORE, "reason": summary[:80]}


def _stance_conflict_reason(content: str, stance_summary: str) -> str:
    if not stance_summary.strip():
        return ""
    normalized_stance = _normalize_for_similarity(stance_summary)
    normalized_content = _normalize_for_similarity(content)
    if _has_marker(normalized_stance, CAUTIOUS_STANCE_MARKERS) and _has_marker(normalized_content, STRONG_POSITIVE_MARKERS):
        return "观望立场不能突然强肯定"
    return ""


def _has_marker(normalized_text: str, markers: tuple[str, ...]) -> bool:
    return any(_normalize_for_similarity(marker) in normalized_text for marker in markers)


def _profile_rejects_emoji(normalized_summary: str) -> bool:
    markers = ("少表情", "不发表情", "不用表情", "不连续发表情", "少emoji", "不用emoji")
    return any(marker in normalized_summary for marker in markers)


def _is_emoji_only_message(content: str) -> bool:
    stripped = re.sub(r"\s+", "", content)
    return bool(stripped) and not EMOJI_PATTERN.sub("", stripped)


def _semantic_cluster(content: str) -> str:
    text = _normalize_for_similarity(content)
    cluster_markers = [
        ("photo_real_match", ("照片准", "照片没p", "照片没修", "没照骗", "真人没差", "本人也差不多", "见面没翻车")),
        ("stable_attitude", ("态度稳", "不催", "不敷衍", "没催", "没加价", "挺省心")),
        ("early_location", ("位置提前", "提前发位置", "发了位置", "没绕路", "没绕远", "跑冤枉路")),
        ("revisit_feedback", ("结束后问", "问反馈", "回访", "下次安排", "下次约不约", "下次啥时候")),
        ("time_punctual", ("准时到", "准点", "时间卡得准", "没干等", "没让我等", "没放鸽子")),
        ("fixed_shell_bonus", ("这点加分", "这点挺加分", "挺加分", "这个加分")),
    ]
    for cluster, markers in cluster_markers:
        if any(_normalize_for_similarity(marker) in text for marker in markers):
            return cluster
    return ""


def _has_unanchored_idle_fact(content: str, *, chat_mode: str, anchor_message_ids: list[int]) -> bool:
    if chat_mode not in {CHAT_MODE_IDLE_WARMUP, CHAT_MODE_BOOTSTRAP}:
        return False
    text = _normalize_for_similarity(content)
    fact_markers = (
        "走之前",
        "结束后",
        "回访",
        "准时到",
        "准点",
        "没让我等",
        "没干等",
        "位置提前",
        "提前发位置",
        "发了位置",
        "位置发过",
        "发过位置",
    )
    normalized_markers = [_normalize_for_similarity(marker) for marker in fact_markers]
    if not any(marker and marker in text for marker in normalized_markers):
        return False
    return True


def _looks_like_fact_claim(content: str) -> bool:
    text = _normalize_for_similarity(content)
    markers = (
        "结束后",
        "走之前",
        "准时到",
        "准点",
        "回访",
        "没让我等",
        "发了位置",
        "位置发过",
        "发过位置",
        "提前发位置",
    )
    return any(_normalize_for_similarity(marker) in text for marker in markers)


def _normalize_for_similarity(content: str) -> str:
    return re.sub(r"[\s，。！？!?、,.；;：:\"'“”‘’（）()\[\]【】]+", "", (content or "").lower())


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _next_cycle_index(session: Session, task: Task) -> int:
    persisted_max = session.scalar(
        select(func.max(ContentMixCycle.cycle_seq)).where(
            ContentMixCycle.task_id == task.id,
        )
    )
    max_index = int(persisted_max or 0)
    rows = session.scalars(
        select(Action.payload["cycle_id"].as_string())
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
        )
        .order_by(Action.created_at.desc())
        .limit(RECENT_CYCLE_SCAN_LIMIT)
    )
    prefix = f"{task.id}:cycle:"
    for cycle_id in rows:
        cycle_id = str(cycle_id or "")
        if not cycle_id.startswith(prefix):
            continue
        try:
            max_index = max(max_index, int(cycle_id.removeprefix(prefix)))
        except ValueError:
            continue
    return max_index + 1


def _role_for_account(account_id: int, index: int, config: dict) -> str:
    personas = config.get("account_personas") if isinstance(config.get("account_personas"), dict) else {}
    role = personas.get(str(account_id)) or personas.get(account_id)
    if role:
        return str(role)
    return _role_for_turn(index)


def _role_for_turn(index: int) -> str:
    roles = ["引导型账号", "补充型账号", "提问型账号", "总结型账号", "轻松闲聊型账号"]
    return roles[index % len(roles)]


def _intent_for_turn(index: int) -> str:
    intents = ["回应上下文", "补充信息", "引出讨论", "轻量总结", "承接话题"]
    return intents[index % len(intents)]


__all__ = ["ai_cycle_mode", "build_plan"]
