from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ChannelDiscussionGroupBinding, OperationTarget, Task

from .channel_comment_discussion_contracts import current_membership_facts, membership_ready


OPEN_ADMISSION_ACTION_STATUSES = frozenset({
    "pending", "claiming", "executing", "retryable_failed",
})


def discussion_admission_candidate_ids(
    session: Session,
    task: Task,
    binding: ChannelDiscussionGroupBinding,
    *,
    accounts: list,
    now_value: datetime,
) -> frozenset[int]:
    config = dict(task.type_config or {})
    if not config.get("auto_join_discussion_enabled"):
        return frozenset()
    _validate_admission_scope(task, binding, config)
    candidates = _joinable_accounts(
        session, task, binding,
        accounts=accounts, now_value=now_value, config=config,
    )
    budget = int(config.get("discussion_join_budget") or 0)
    return frozenset(int(account.id) for account in candidates[:budget])


def ensure_discussion_membership_actions(
    session: Session,
    task: Task,
    binding: ChannelDiscussionGroupBinding,
    *,
    accounts: list,
    now_value: datetime,
) -> dict[int, Action]:
    _lock_admission_owner(session, task)
    config = dict(task.type_config or {})
    if not config.get("auto_join_discussion_enabled"):
        return {}
    _validate_admission_scope(task, binding, config)
    candidates = _joinable_accounts(
        session, task, binding,
        accounts=accounts, now_value=now_value, config=config,
    )
    budget = int(config.get("discussion_join_budget") or 0)
    scope_hash = discussion_join_scope_hash(config, binding)
    actions: dict[int, Action] = {}
    for ordinal, account in enumerate(candidates[:budget], 1):
        account_id = int(account.id)
        action = _ensure_membership_action(
            session, task, binding,
            account_id=account_id,
            ordinal=ordinal,
            scope_hash=scope_hash,
            config=config,
            now_value=now_value,
        )
        if action is not None:
            actions[account_id] = action
    session.flush()
    return actions


def _lock_admission_owner(session: Session, task: Task) -> None:
    owner = session.scalar(select(Task.id).where(
        Task.id == task.id,
        Task.tenant_id == task.tenant_id,
    ).with_for_update())
    if owner is None:
        raise ValueError("discussion_admission_task_missing")
    session.refresh(task)


def _joinable_accounts(
    session: Session,
    task: Task,
    binding: ChannelDiscussionGroupBinding,
    *,
    accounts: list,
    now_value: datetime,
    config: dict,
) -> list:
    authorized = {int(value) for value in config.get("discussion_join_account_ids") or []}
    account_ids = [int(account.id) for account in accounts if int(account.id) in authorized]
    facts = current_membership_facts(
        session, tenant_id=task.tenant_id, account_ids=account_ids,
        discussion_peer_id=str(binding.discussion_peer_id), group_binding_id=binding.id,
    )
    keys = [_membership_dedupe_key(task, binding, account_id) for account_id in account_ids]
    actions = {row.action_dedupe_key: row for row in session.scalars(select(Action).where(
        Action.tenant_id == task.tenant_id, Action.action_dedupe_key.in_(keys),
    ))} if keys else {}
    eligible = []
    for account in accounts:
        account_id = int(account.id)
        if account_id not in authorized:
            continue
        fact = facts.get(account_id)
        if not _membership_requires_admission(fact, now_value):
            continue
        existing = actions.get(_membership_dedupe_key(task, binding, account_id))
        if existing is not None and existing.status not in OPEN_ADMISSION_ACTION_STATUSES:
            continue
        eligible.append(account)
    return eligible


def _validate_admission_scope(
    task: Task,
    binding: ChannelDiscussionGroupBinding,
    config: dict,
) -> None:
    if task.status != "running" or not binding.is_current or binding.binding_status != "active":
        raise ValueError("discussion_admission_scope_stale")
    if not binding.discussion_target_id:
        raise ValueError("discussion_operation_target_missing")
    if not config.get("discussion_join_account_ids"):
        raise ValueError("discussion_join_authorized_scope_required")
    if int(config.get("discussion_join_budget") or 0) <= 0:
        raise ValueError("discussion_join_budget_required")
    if not str(config.get("discussion_join_pacing_policy_version") or "").strip():
        raise ValueError("discussion_join_pacing_policy_version_required")
    pacing_policy = dict(config.get("discussion_join_pacing_policy") or {})
    if int(pacing_policy.get("interval_seconds") or 0) <= 0:
        raise ValueError("discussion_join_pacing_policy_required")


def _ensure_membership_action(
    session: Session,
    task: Task,
    binding: ChannelDiscussionGroupBinding,
    *,
    account_id: int,
    ordinal: int,
    scope_hash: str,
    config: dict,
    now_value: datetime,
) -> Action | None:
    existing = _membership_action(
        session, task, binding, account_id=account_id,
    )
    if existing is not None:
        return (
            existing
            if existing.status in OPEN_ADMISSION_ACTION_STATUSES
            else None
        )
    interval = int(config["discussion_join_pacing_policy"]["interval_seconds"])
    target = session.get(OperationTarget, binding.discussion_target_id)
    if target is None:
        raise ValueError("discussion_operation_target_missing")
    action = Action(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_type=task.type,
        action_type="ensure_discussion_membership",
        account_id=account_id,
        scheduled_at=now_value + timedelta(seconds=interval * (ordinal - 1)),
        status="pending",
        action_dedupe_key=_membership_dedupe_key(task, binding, account_id),
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        execution_lane="admission",
        payload=_membership_payload(
            task, binding, target=target, scope_hash=scope_hash,
            pacing_policy=config["discussion_join_pacing_policy"],
            pacing_policy_version=config["discussion_join_pacing_policy_version"],
            ordinal=ordinal,
        ),
    )
    session.add(action)
    return action


def _membership_action(
    session: Session,
    task: Task,
    binding: ChannelDiscussionGroupBinding,
    *,
    account_id: int,
) -> Action | None:
    return session.scalar(select(Action).where(
        Action.tenant_id == task.tenant_id,
        Action.action_dedupe_key == _membership_dedupe_key(
            task, binding, account_id,
        ),
    ))


def _membership_payload(
    task: Task,
    binding: ChannelDiscussionGroupBinding,
    *,
    target: OperationTarget,
    scope_hash: str,
    pacing_policy: dict,
    pacing_policy_version: str,
    ordinal: int,
) -> dict:
    return {
        "discussion_peer_id": binding.discussion_peer_id,
        "target_operation_target_id": target.id,
        "target_reference_revision": target.reference_revision,
        "target_reference_snapshot": {
            "tg_peer_id": target.tg_peer_id,
            "title": target.title,
            "username": target.username,
        },
        "target_type": "group",
        "target_display": target.title,
        "discussion_group_binding_id": binding.id,
        "discussion_group_binding_revision": binding.binding_revision,
        "discussion_group_identity_hash": binding.identity_hash,
        "task_config_revision": task.config_revision,
        "task_lifecycle_epoch": task.task_lifecycle_epoch,
        "authorized_scope_hash": scope_hash,
        "pacing_policy_version": pacing_policy_version,
        "pacing_policy_hash": _stable_hash(pacing_policy),
        "join_budget_ordinal": ordinal,
    }


def discussion_join_scope_hash(config: dict, binding: ChannelDiscussionGroupBinding) -> str:
    return _stable_hash({
        "account_ids": sorted(int(value) for value in config.get("discussion_join_account_ids") or []),
        "binding_id": binding.id,
        "binding_revision": binding.binding_revision,
        "pacing_policy": config.get("discussion_join_pacing_policy") or {},
        "pacing_policy_version": config.get("discussion_join_pacing_policy_version") or "",
        "join_budget": int(config.get("discussion_join_budget") or 0),
    })


def _membership_dedupe_key(
    task: Task,
    binding: ChannelDiscussionGroupBinding,
    account_id: int,
) -> str:
    return ":".join((
        "ensure_discussion_membership", task.id,
        str(task.task_lifecycle_epoch), str(task.config_revision),
        str(account_id), str(binding.binding_revision),
    ))


def _membership_forbidden(fact) -> bool:
    return bool(fact and fact.membership_status in {"restricted", "banned", "inaccessible"})


def _membership_requires_admission(fact, now_value: datetime) -> bool:
    if membership_ready(fact, now_value) or _membership_forbidden(fact):
        return False
    return fact is None or fact.membership_status == "not_participant"


def _stable_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = [
    "discussion_admission_candidate_ids",
    "discussion_join_scope_hash",
    "ensure_discussion_membership_actions",
]
