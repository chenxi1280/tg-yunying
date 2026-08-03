from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import random

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    BotProtocolSample,
    AccountEnvironmentBinding,
    AccountProxyBinding,
    OperationTarget,
    SearchClickOpportunityAssignment,
    SearchClickFulfillmentObligation,
    Task,
    TaskDayLedger,
    TgAccount,
    TgAccountAuthorization,
    DispatchClaimScope,
    DispatchRuntimeShardState,
)
from app.search_join_protocol import pure_click_protocol_profile_is_approved
from app.services.account_capacity import (
    account_capacity_decision,
    account_hard_safe_remaining_capacity,
)

from ..account_pool import select_task_accounts
from ..jisou_selector_accounts import select_jisou_selector_candidates
from ..search_click_assignment_solver import SearchClickCandidatePath
from ..search_click_dispatch_allocation import SearchClickFulfillmentUnit
from ..dispatch_runtime_contract import (
    build_dispatch_runtime_contract,
    live_shard_indexes,
    require_active_scope_contract,
)
from .search_join_group import (
    PayloadInput,
    SearchJoinPlan,
    _approved_protocol_profile,
    _environment,
    _first_bot_username,
    _keyword_materials,
    _protocol_sample,
    _target,
)


@dataclass(frozen=True)
class SearchClickPathContext:
    candidate: SearchClickCandidatePath
    payload_input: PayloadInput


@dataclass(frozen=True)
class _TaskPathBase:
    config: dict
    bot_username: str
    sample: BotProtocolSample
    profile: dict
    target: OperationTarget
    materials: list[tuple[str, str]]


@dataclass(frozen=True)
class _AccountPathInput:
    session: Session
    task: Task
    account: TgAccount
    units: tuple[SearchClickFulfillmentUnit, ...]
    base: _TaskPathBase
    keyword_hash: str
    now: datetime
    blockers: dict[str, int]
    random_order: int


def candidate_paths(
    session: Session,
    units: tuple[SearchClickFulfillmentUnit, ...],
    now_value: datetime,
) -> dict[str, SearchClickPathContext]:
    task_ids = sorted({unit.task_id for unit in units})
    tasks = list(session.scalars(
        select(Task).where(Task.id.in_(task_ids), Task.status == "running")
    ))
    result: dict[str, SearchClickPathContext] = {}
    for task in tasks:
        task_units = tuple(unit for unit in units if unit.task_id == task.id)
        for context in _task_candidate_paths(session, task, task_units, now_value):
            result[context.candidate.key] = context
    return result


def store_capacity_projection(
    session: Session,
    units: tuple[SearchClickFulfillmentUnit, ...],
    paths: dict[str, SearchClickPathContext],
) -> None:
    task_by_obligation = {
        unit.obligation_id: unit.task_id
        for unit in units
    }
    capacity_by_task: dict[str, int] = {}
    for context in paths.values():
        task_ids = {
            task_by_obligation[obligation_id]
            for obligation_id in context.candidate.eligible_obligation_ids
            if obligation_id in task_by_obligation
        }
        for task_id in task_ids:
            capacity_by_task[task_id] = (
                capacity_by_task.get(task_id, 0)
                + context.candidate.hard_safe_remaining_capacity
            )
    for task_id in sorted({unit.task_id for unit in units}):
        _store_task_capacity(session, task_id, capacity_by_task.get(task_id, 0))


def _store_task_capacity(session: Session, task_id: str, capacity: int) -> None:
    task = session.get(Task, task_id)
    if task is None:
        return
    stats = dict(task.stats or {})
    stats["projected_eligible_attempt_capacity"] = capacity
    stats["hard_safe_attempt_capacity"] = capacity
    stats["projection_not_reserved"] = True
    task.stats = stats


def _task_candidate_paths(
    session: Session,
    task: Task,
    units: tuple[SearchClickFulfillmentUnit, ...],
    now_value: datetime,
) -> tuple[SearchClickPathContext, ...]:
    base = _task_path_base(session, task)
    if base is None:
        return ()
    accounts = select_task_accounts(
        session,
        task.tenant_id,
        task.account_config or {},
        enforce_capacity=False,
        scan_all_candidates=True,
    )
    accounts = _select_candidates(session, task, accounts, base, now_value)
    accounts = _live_runtime_shard_accounts(
        session,
        task,
        accounts,
        now_value,
    )
    return _account_contexts(session, task, units, base, accounts, now_value)


def _select_candidates(session, task, accounts, base, now_value):
    if task.fulfillment_contract_version == "fact_first_v3":
        shuffled = list(accounts)
        random.SystemRandom().shuffle(shuffled)
        return tuple(shuffled)
    selection = select_jisou_selector_candidates(
        session,
        task,
        accounts,
        bot_username=base.bot_username,
        now_value=now_value,
    )
    return selection.accounts


def _live_runtime_shard_accounts(
    session: Session,
    task: Task,
    accounts: tuple[TgAccount, ...],
    now_value: datetime,
) -> tuple[TgAccount, ...]:
    if task.fulfillment_contract_version == "fact_first_v3":
        return accounts
    settings = get_settings()
    scope = session.scalar(select(DispatchClaimScope).where(
        DispatchClaimScope.dispatcher_scope == settings.dispatcher_claim_scope,
    ))
    if scope is None or not scope.topology_fingerprint:
        return accounts
    contract = build_dispatch_runtime_contract(settings)
    require_active_scope_contract(scope, contract)
    states = list(session.scalars(select(DispatchRuntimeShardState).where(
        DispatchRuntimeShardState.dispatcher_scope == scope.dispatcher_scope,
    )))
    live = live_shard_indexes(
        states,
        contract,
        now=now_value,
        stale_seconds=int(settings.dispatch_shard_stale_seconds),
    )
    filtered = tuple(
        account for account in accounts
        if account.id % contract.runtime_shard_total in live
    )
    if accounts and not filtered:
        _record_blocker(task, "dispatcher_shard_unavailable")
    return filtered


def _task_path_base(
    session: Session,
    task: Task,
) -> _TaskPathBase | None:
    config = dict(task.type_config or {})
    bot_username = _first_bot_username(config)
    sample = _protocol_sample(session, task.tenant_id, bot_username)
    profile = _approved_protocol_profile(sample, bot_username) if sample else None
    target = _target(session, task)
    materials = _keyword_materials(config)
    if not sample or not profile:
        _record_blocker(task, "protocol_sample_missing")
        return None
    if not pure_click_protocol_profile_is_approved(sample.structure_json):
        _record_blocker(task, "click_only_membership_effect_unproven")
        return None
    if target is None or not materials:
        _record_blocker(task, "search_click_structure_runtime_invalid")
        return None
    return _TaskPathBase(
        config,
        bot_username,
        sample,
        profile,
        target,
        materials,
    )


def _account_contexts(
    session: Session,
    task: Task,
    units: tuple[SearchClickFulfillmentUnit, ...],
    base: _TaskPathBase,
    accounts: tuple[TgAccount, ...],
    now_value: datetime,
) -> tuple[SearchClickPathContext, ...]:
    blockers: dict[str, int] = {}
    contexts: list[SearchClickPathContext] = []
    for index, account in enumerate(accounts):
        value = _AccountPathInput(
            session,
            task,
            account,
            units,
            base,
            base.materials[index % len(base.materials)][0],
            now_value,
            blockers,
            index,
        )
        context = _account_path_context(value)
        if context is not None:
            contexts.append(context)
    if not contexts:
        _record_blocker(task, next(iter(blockers), "search_click_no_candidate"))
    return tuple(contexts)


def _account_path_context(
    value: _AccountPathInput,
) -> SearchClickPathContext | None:
    if value.task.fulfillment_contract_version != "fact_first_v3":
        decision = account_capacity_decision(
            value.session,
            tenant_id=value.task.tenant_id,
            account_id=value.account.id,
            scheduled_at=value.now,
        )
        if not decision.available:
            value.blockers[decision.reason_code] = (
                value.blockers.get(decision.reason_code, 0) + 1
            )
            return None
    environment = _environment(value.session, value.account, value.blockers)
    if environment is None:
        return None
    candidate = _candidate(value, environment)
    plan = SearchJoinPlan(
        value.base.bot_username,
        value.keyword_hash,
        value.base.target,
        {},
        value.base.sample.schema_version,
        value.base.profile,
    )
    payload_input = PayloadInput(
        value.base.config,
        plan,
        value.keyword_hash,
        value.account,
        environment,
    )
    return SearchClickPathContext(candidate, payload_input)


def _candidate(value: _AccountPathInput, environment) -> SearchClickCandidatePath:
    candidate_key = ":".join((
        value.task.id,
        str(value.account.id),
        str(environment.authorization_id),
        value.keyword_hash,
    ))
    capacity = _candidate_capacity(value)
    return SearchClickCandidatePath(
        key=candidate_key,
        account_id=value.account.id,
        authorization_id=environment.authorization_id,
        keyword_hash=value.keyword_hash,
        proxy_route_id=str(environment.proxy_binding_id),
        protocol_sample_version=value.base.sample.schema_version,
        hard_safe_remaining_capacity=capacity,
        confirmed_click_count_today=_confirmed_for_account(
            value.session, value.account.id, value.now
        ),
        last_click_opportunity_at=_last_opportunity_at(
            value.session, value.account.id
        ),
        persistent_account_cursor=(
            value.random_order
            if value.task.fulfillment_contract_version == "fact_first_v3"
            else value.account.id
        ),
        eligible_obligation_ids=tuple(unit.obligation_id for unit in value.units),
        resource_versions=_resource_versions(value, environment, capacity),
    )


def _candidate_capacity(value: _AccountPathInput) -> int:
    if value.task.fulfillment_contract_version == "fact_first_v3":
        return 1
    return account_hard_safe_remaining_capacity(
        value.session,
        tenant_id=value.task.tenant_id,
        account_id=value.account.id,
        scheduled_at=value.now,
        max_needed=len(value.units),
    )


def _confirmed_for_account(
    session: Session,
    account_id: int,
    now_value: datetime,
) -> int:
    return int(session.scalar(
        select(func.count(SearchClickOpportunityAssignment.id)).where(
            SearchClickOpportunityAssignment.account_id == account_id,
            SearchClickOpportunityAssignment.obligation_id
            == SearchClickFulfillmentObligation.id,
            SearchClickFulfillmentObligation.task_day_ledger_id
            == TaskDayLedger.id,
            SearchClickFulfillmentObligation.status == "confirmed",
            TaskDayLedger.period_start_at <= now_value,
            TaskDayLedger.deadline_at > now_value,
        )
    ) or 0)


def _last_opportunity_at(
    session: Session,
    account_id: int,
) -> datetime | None:
    return session.scalar(
        select(func.max(SearchClickOpportunityAssignment.created_at)).where(
            SearchClickOpportunityAssignment.account_id == account_id,
        )
    )


def _resource_versions(
    value: _AccountPathInput,
    environment,
    capacity: int,
) -> tuple[tuple[str, str, str], ...]:
    authorization = value.session.get(
        TgAccountAuthorization, environment.authorization_id
    )
    binding = value.session.get(AccountEnvironmentBinding, environment.binding_id)
    proxy_binding = value.session.get(
        AccountProxyBinding, environment.proxy_binding_id
    )
    resources = (
        ("account", str(value.account.id), _hash({
            "status": value.account.status,
            "identity": value.account.account_identity,
            "deleted_at": _iso(value.account.deleted_at),
            "developer_app_version": value.account.developer_app_version,
        })),
        ("authorization", str(environment.authorization_id), _hash({
            "status": authorization.status if authorization else None,
            "health": authorization.health_status if authorization else None,
            "updated_at": _iso(authorization.updated_at) if authorization else None,
        })),
        ("environment", str(environment.binding_id), _hash({
            "status": binding.status if binding else None,
            "updated_at": _iso(binding.updated_at) if binding else None,
            "client_identity_key": (
                binding.client_identity_key if binding else None
            ),
        })),
        ("proxy_binding", str(environment.proxy_binding_id), _hash({
            "status": proxy_binding.status if proxy_binding else None,
            "generation": (
                proxy_binding.binding_generation if proxy_binding else None
            ),
            "exit_ip": (
                proxy_binding.observed_exit_ip if proxy_binding else None
            ),
        })),
        ("protocol", value.base.sample.id, _hash({
            "schema": value.base.sample.schema_version,
            "sample_hash": value.base.sample.sample_hash,
            "captured_at": _iso(value.base.sample.captured_at),
        })),
        ("account_capacity", str(value.account.id), str(capacity)),
        ("gateway_contract", "pure_click", "v1"),
    )
    return tuple(sorted(resources))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _record_blocker(task: Task, code: str) -> None:
    stats = dict(task.stats or {})
    stats["search_click_runtime_blocker"] = code
    task.stats = stats
    task.last_error = code


__all__ = [
    "SearchClickPathContext",
    "candidate_paths",
    "store_capacity_projection",
]
