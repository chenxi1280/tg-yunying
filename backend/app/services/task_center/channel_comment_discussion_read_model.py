from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountStatus,
    ChannelMessage,
    ListenerSourceState,
    Task,
    TaskSourceSubscription,
    TgAccount,
)

from .account_pool import select_task_accounts
from .channel_comment_discussion_admission import discussion_admission_candidate_ids
from .channel_comment_discussion_contracts import current_group_binding
from .channel_comment_grounding_enrollment import latest_grounding_enrollment
from .channel_comment_discussion_freshness import group_binding_fresh
from .channel_comment_discussion_guard import discussion_membership_counts
from .channel_membership import candidate_accounts_for_config
from .comment_account_profiles import comment_account_profile_ready


def channel_comment_discussion_read_model(
    session: Session,
    task: Task,
    *,
    now_value: datetime | None = None,
) -> dict:
    if task.type != "channel_comment":
        return {}
    observed_at = now_value or datetime.now(timezone.utc)
    selection = dict(task.account_config or {})
    base_accounts = candidate_accounts_for_config(session, task.tenant_id, selection)
    execution_accounts = _execution_candidates(session, task)
    target_id = int((task.type_config or {}).get("target_channel_id") or 0)
    binding = current_group_binding(session, task.tenant_id, target_id) if target_id else None
    membership = _membership_projection(
        session, task, binding=binding,
        base_accounts=base_accounts, execution_accounts=execution_accounts,
        observed_at=observed_at,
    )
    return {
        "selection_mode": str(selection.get("selection_mode") or "all"),
        "configured_account_ids": _configured_account_ids(selection),
        "raw_online_count": _raw_online_count(session, task.tenant_id),
        "base_operational_candidate_count": len(base_accounts),
        **membership,
        "binding": _binding_payload(session, binding, observed_at),
        "enrollment": _enrollment_payload(
            latest_grounding_enrollment(session, task), task,
        ),
        "listener": _listener_payload(session, task, target_id),
    }


def _execution_candidates(session: Session, task: Task) -> list:
    accounts = select_task_accounts(
        session,
        task.tenant_id,
        task.account_config or {},
        limit=1,
        enforce_max_concurrent=False,
        enforce_capacity=True,
        scan_all_candidates=True,
    )
    return [account for account in accounts if comment_account_profile_ready(account)]


def _membership_projection(
    session: Session,
    task: Task,
    *,
    binding,
    base_accounts: list,
    execution_accounts: list,
    observed_at: datetime,
) -> dict:
    empty = {
        "discussion_membership_ready_count": 0,
        "discussion_admission_required_count": len(base_accounts),
        "discussion_forbidden_count": 0,
        "discussion_membership_unknown_count": 0,
        "comment_contract_eligible_count": 0,
        "effective_comment_ready_count": 0,
    }
    if binding is None or binding.binding_status != "active":
        return empty
    base_ids = [int(account.id) for account in base_accounts]
    counts = discussion_membership_counts(
        session, task, binding,
        account_ids=base_ids, now_value=observed_at,
    )
    candidates = discussion_admission_candidate_ids(
        session, task, binding,
        accounts=base_accounts, now_value=observed_at,
    )
    effective_ids = [int(account.id) for account in execution_accounts]
    effective = discussion_membership_counts(
        session, task, binding,
        account_ids=effective_ids, now_value=observed_at,
    )
    return {
        **counts,
        "comment_contract_eligible_count": counts["discussion_membership_ready_count"] + len(candidates),
        "effective_comment_ready_count": effective["discussion_membership_ready_count"],
    }


def _configured_account_ids(config: dict) -> list[int]:
    if str(config.get("selection_mode") or "all") != "manual":
        return []
    return sorted({int(value) for value in config.get("account_ids") or []})


def _raw_online_count(session: Session, tenant_id: int) -> int:
    return int(session.scalar(select(func.count(TgAccount.id)).where(
        TgAccount.tenant_id == tenant_id,
        TgAccount.deleted_at.is_(None),
        TgAccount.status == AccountStatus.ACTIVE.value,
    )) or 0)


def _binding_payload(session: Session, binding, observed_at: datetime) -> dict:
    if binding is None:
        return {"status": "unproven", "fresh": False}
    return {
        "id": binding.id,
        "revision": binding.binding_revision,
        "status": binding.binding_status,
        "channel_peer_id": binding.channel_peer_id,
        "discussion_peer_id": binding.discussion_peer_id,
        "identity_hash": binding.identity_hash,
        "fresh": group_binding_fresh(session, binding, observed_at),
    }


def _enrollment_payload(enrollment, task: Task) -> dict:
    if enrollment is None:
        return {"state": "not_enrolled"}
    current_identity = (
        enrollment.task_config_revision == task.config_revision
        and enrollment.task_lifecycle_epoch == task.task_lifecycle_epoch
    )
    state = enrollment.enrollment_state if current_identity else "stale_task_revision"
    return {
        "id": enrollment.id,
        "revision": enrollment.enrollment_revision,
        "state": state,
        "row_state": enrollment.enrollment_state,
        "contract_version": enrollment.contract_version,
        "enabled_at": enrollment.enabled_at,
        "closed_at": enrollment.closed_at,
        "task_config_revision": enrollment.task_config_revision,
        "task_lifecycle_epoch": enrollment.task_lifecycle_epoch,
    }


def _listener_payload(session: Session, task: Task, target_id: int) -> dict:
    subscription = session.scalar(select(TaskSourceSubscription).where(
        TaskSourceSubscription.task_id == task.id,
        TaskSourceSubscription.lifecycle_epoch == task.task_lifecycle_epoch,
    ).order_by(TaskSourceSubscription.created_at.desc()))
    state = session.get(ListenerSourceState, subscription.listener_source_state_id) if subscription else None
    last_published = session.scalar(select(func.max(ChannelMessage.published_at)).where(
        ChannelMessage.tenant_id == task.tenant_id,
        ChannelMessage.channel_target_id == target_id,
    )) if target_id else None
    return {
        "snapshot_state": state.snapshot_status if state else "pending",
        "last_collected_at": state.observed_at if state else None,
        "last_message_published_at": last_published,
        "subscription_id": subscription.id if subscription else None,
        "target_reference_revision": subscription.target_reference_revision if subscription else None,
        "listener_revision": subscription.listener_revision if subscription else None,
        "error_code": state.last_error_code if state else "channel_source_snapshot_pending",
    }


__all__ = ["channel_comment_discussion_read_model"]
