from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelMessage,
    CommentFulfillmentObligation,
    ContentMixContract,
    RuleSetVersion,
    Task,
)


OPEN_OBLIGATION_STATUSES = frozenset({"open", "replan_required"})


def freeze_comment_obligations(
    session: Session,
    task: Task,
    message: ChannelMessage,
    *,
    reply_targets: list[dict | None],
    rule_version: RuleSetVersion,
    reply_min_required: int,
    first_ordinal: int = 1,
    plan_contract_id: str | None = None,
    revision_override: int | None = None,
    account_by_ordinal: dict[int, int] | None = None,
    grounding_assignment_by_ordinal: dict[int, object] | None = None,
    planned_fallback_ordinals: set[int] | None = None,
    discussion_identity: object | None = None,
) -> list[CommentFulfillmentObligation]:
    existing = _message_obligations(session, task, message.id)
    reusable, remaining = _take_reusable_obligations(session, existing, reply_targets)
    if not remaining:
        return reusable
    rows = _append_comment_obligations(
        session, task, message, reply_targets=remaining,
        rule_version=rule_version,
        reply_min_required=reply_min_required,
        first_ordinal=first_ordinal,
        plan_contract_id=plan_contract_id,
        revision_override=revision_override,
        account_by_ordinal=account_by_ordinal or {},
        grounding_assignment_by_ordinal=grounding_assignment_by_ordinal or {},
        planned_fallback_ordinals=planned_fallback_ordinals or set(),
        discussion_identity=discussion_identity,
    )
    return [*reusable, *rows]


def _append_comment_obligations(
    session: Session,
    task: Task,
    message: ChannelMessage,
    *,
    reply_targets: list[dict | None],
    rule_version: RuleSetVersion,
    reply_min_required: int,
    first_ordinal: int,
    plan_contract_id: str | None,
    revision_override: int | None,
    account_by_ordinal: dict[int, int],
    grounding_assignment_by_ordinal: dict[int, object],
    planned_fallback_ordinals: set[int],
    discussion_identity: object | None,
) -> list[CommentFulfillmentObligation]:
    revision = int(revision_override or task.config_revision or 1)
    contract = _create_comment_contract(
        session, task, message, targets=reply_targets,
        rule_version=rule_version,
        revision=revision,
        reply_min_required=reply_min_required,
    )
    _freeze_fallback_contract(
        session, task, message, contract=contract,
        comment_plan_revision=revision,
    )
    rows = _new_obligations(
        task,
        message,
        contract,
        revision=revision,
        first_ordinal=first_ordinal,
        reply_targets=reply_targets,
        plan_contract_id=plan_contract_id,
        account_by_ordinal=account_by_ordinal,
        grounding_assignment_by_ordinal=grounding_assignment_by_ordinal,
        planned_fallback_ordinals=planned_fallback_ordinals,
        discussion_identity=discussion_identity,
    )
    _freeze_assignment_relations(rows, grounding_assignment_by_ordinal)
    session.add_all(rows)
    session.flush()
    return rows


def _freeze_assignment_relations(
    obligations: list[CommentFulfillmentObligation],
    assignments: dict[int, object],
) -> None:
    for obligation in obligations:
        assignment = assignments.get(int(obligation.target_ordinal))
        if assignment is None:
            continue
        assignment.relation_kind = obligation.relation_kind


def _take_reusable_obligations(
    session: Session,
    existing: list[CommentFulfillmentObligation],
    targets: list[dict | None],
) -> tuple[list[CommentFulfillmentObligation], list[dict | None]]:
    if not existing:
        return [], targets
    _release_terminal_bindings(session, existing)
    available = [
        item for item in existing
        if item.status in OPEN_OBLIGATION_STATUSES and item.current_action_id is None
    ]
    return _reuse_available_obligations(available, targets) if available else ([], targets)


def _freeze_fallback_contract(
    session: Session,
    task: Task,
    message: ChannelMessage,
    *,
    contract: ContentMixContract,
    comment_plan_revision: int,
) -> None:
    from .comment_fallback_selection import freeze_comment_fallback_contract

    freeze_comment_fallback_contract(
        session,
        task,
        channel_message_id=message.id,
        comment_plan_revision=comment_plan_revision,
        content_mix_contract_id=contract.id,
    )


def _new_obligations(
    task: Task,
    message: ChannelMessage,
    contract: ContentMixContract,
    *,
    revision: int,
    first_ordinal: int,
    reply_targets: list[dict | None],
    plan_contract_id: str | None,
    account_by_ordinal: dict[int, int],
    grounding_assignment_by_ordinal: dict[int, object],
    planned_fallback_ordinals: set[int],
    discussion_identity: object | None,
) -> list[CommentFulfillmentObligation]:
    return [
        _new_obligation(
            task,
            message,
            contract,
            revision=revision,
            ordinal=index,
            reply_target=target,
            plan_contract_id=plan_contract_id,
            account_id=account_by_ordinal.get(index),
            grounding_assignment_id=getattr(
                grounding_assignment_by_ordinal.get(index), "id", None,
            ),
            fallback_intent_kind=(
                "planned" if index in planned_fallback_ordinals else "emergency"
            ),
            discussion_identity=discussion_identity,
        )
        for index, target in enumerate(reply_targets, start=first_ordinal)
    ]


def _reuse_available_obligations(
    available: list[CommentFulfillmentObligation],
    reply_targets: list[dict | None],
) -> tuple[list[CommentFulfillmentObligation], list[dict | None]]:
    remaining = list(reply_targets)
    selected: list[CommentFulfillmentObligation] = []
    for obligation in available[:len(reply_targets)]:
        target_index = _matching_target_index(remaining, obligation.relation_kind)
        if target_index is None:
            if remaining:
                remaining.pop(0)
            continue
        target = remaining.pop(target_index)
        _refresh_obligation_target(obligation, target)
        selected.append(obligation)
    return selected, remaining


def _matching_target_index(
    targets: list[dict | None],
    relation_kind: str,
) -> int | None:
    reply_required = relation_kind == "reply"
    return next(
        (
            index
            for index, target in enumerate(targets)
            if (target is not None) == reply_required
        ),
        None,
    )


def _refresh_obligation_target(
    obligation: CommentFulfillmentObligation,
    target: dict | None,
) -> None:
    snapshot = dict(target or {})
    obligation.reply_target_snapshot = snapshot
    obligation.reply_to_message_id = _reply_target_id(snapshot)


def _release_terminal_bindings(
    session: Session,
    obligations: list[CommentFulfillmentObligation],
) -> None:
    for obligation in obligations:
        if not obligation.current_action_id:
            continue
        action = session.get(Action, obligation.current_action_id)
        if action is None or action.status not in {
            "cancelled",
            "failed",
            "skipped",
            "retryable_failed",
        }:
            continue
        obligation.current_action_id = None
        obligation.status = "replan_required"


def bind_comment_obligation(
    session: Session,
    obligation: CommentFulfillmentObligation,
    action: Action,
) -> None:
    if obligation.status not in OPEN_OBLIGATION_STATUSES:
        raise ValueError("comment_obligation_not_open")
    if obligation.current_action_id:
        raise ValueError("comment_obligation_already_bound")
    obligation.current_action_id = action.id
    obligation.action_attempt_no += 1
    obligation.status = "pending"
    session.flush()


def _message_obligations(
    session: Session,
    task: Task,
    message_id: int,
) -> list[CommentFulfillmentObligation]:
    return list(session.scalars(
        select(CommentFulfillmentObligation)
        .where(
            CommentFulfillmentObligation.task_id == task.id,
            CommentFulfillmentObligation.channel_message_id == message_id,
        )
        .order_by(CommentFulfillmentObligation.target_ordinal)
    ))


def _create_comment_contract(
    session: Session,
    task: Task,
    message: ChannelMessage,
    *,
    targets: list[dict | None],
    rule_version: RuleSetVersion,
    revision: int,
    reply_min_required: int,
) -> ContentMixContract:
    reply_count = sum(target is not None for target in targets)
    scope_key = f"comment:{task.id}:{message.id}:{revision}"
    contract = ContentMixContract(
        tenant_id=task.tenant_id,
        content_mix_scope_key=scope_key,
        content_contract_version=_next_contract_version(session, scope_key),
        scope_total_slots=len(targets),
        allocation_seed=f"{task.id}:{message.id}:{revision}",
        reply_min_required_count=min(reply_min_required, len(targets)),
        reply_planned_count=reply_count,
        direct_planned_count=len(targets) - reply_count,
        material_policy_rule_set_id=str(rule_version.rule_set_id),
        material_policy_rule_set_version=int(rule_version.version),
        target_resolution_trace=json.dumps(
            {
                "channel_message_id": message.id,
                "channel_target_id": message.channel_target_id,
                "telegram_message_id": message.message_id,
            },
            sort_keys=True,
        ),
    )
    session.add(contract)
    session.flush()
    return contract


def _next_contract_version(session: Session, scope_key: str) -> int:
    current = session.scalar(
        select(func.max(ContentMixContract.content_contract_version)).where(
            ContentMixContract.content_mix_scope_key == scope_key
        )
    )
    return int(current or 0) + 1


def _new_obligation(
    task: Task,
    message: ChannelMessage,
    contract: ContentMixContract,
    *,
    revision: int,
    ordinal: int,
    reply_target: dict | None,
    plan_contract_id: str | None,
    account_id: int | None,
    grounding_assignment_id: str | None,
    fallback_intent_kind: str,
    discussion_identity: object | None,
) -> CommentFulfillmentObligation:
    snapshot = dict(reply_target or {})
    reply_id = _reply_target_id(snapshot)
    account_id_value = account_id
    return CommentFulfillmentObligation(
        tenant_id=task.tenant_id,
        task_id=task.id,
        channel_message_id=message.id,
        comment_plan_revision=revision,
        target_ordinal=ordinal,
        content_mix_contract_id=contract.id,
        plan_contract_id=plan_contract_id,
        account_id=account_id_value,
        grounding_assignment_id=grounding_assignment_id,
        fallback_intent_kind=fallback_intent_kind,
        relation_kind="reply" if reply_id else "direct",
        reply_to_message_id=reply_id,
        reply_target_snapshot=snapshot,
        **_obligation_discussion_fields(
            task, message,
            discussion_identity=discussion_identity,
            account_id=account_id_value,
            reply_id=reply_id,
        ),
        status="open",
    )


def _obligation_discussion_fields(
    task: Task,
    message: ChannelMessage,
    *,
    discussion_identity: object | None,
    account_id: int | None,
    reply_id: int | None,
) -> dict:
    if discussion_identity is None:
        return {}
    enrollment = discussion_identity.enrollment
    binding = discussion_identity.group_binding
    thread = discussion_identity.thread_binding
    membership = discussion_identity.membership_by_account.get(int(account_id or 0))
    if membership is None and not discussion_identity.freeze_pending_memberships:
        raise ValueError("channel_comment_membership_fact_not_frozen")
    return {
        "grounding_enrollment_id": enrollment.id,
        "discussion_group_binding_id": binding.id,
        "discussion_group_binding_revision": binding.binding_revision,
        "discussion_group_identity_hash": binding.identity_hash,
        "discussion_thread_binding_id": thread.id,
        "discussion_thread_revision": thread.thread_revision,
        "discussion_thread_identity_hash": thread.identity_hash,
        "rpc_mode": "discussion_reply_to" if reply_id else "channel_comment_to",
        "channel_peer_id": binding.channel_peer_id,
        "discussion_peer_id": thread.discussion_peer_id,
        "source_remote_message_id": message.message_id,
        "thread_root_message_id": thread.thread_root_message_id,
        "membership_fact_id": membership.id if membership is not None else None,
        "task_lifecycle_epoch": task.task_lifecycle_epoch,
        "task_config_revision": task.config_revision,
    }


def _reply_target_id(snapshot: dict) -> int | None:
    return int(
        snapshot.get("message_id")
        or snapshot.get("comment_message_id")
        or 0
    ) or None


def clean_expired_comment_obligations(
    session: Session,
    task: Task,
    *,
    now_value=None,
) -> int:
    from app.services._common import _now
    from .fulfillment_activation import CURRENT_CONTRACT_VERSION
    from .channel_comment_source import comment_source_window
    if getattr(task, "fulfillment_contract_version", None) != CURRENT_CONTRACT_VERSION:
        return 0
    now_val = now_value or _now()
    rows = session.scalars(
        select(CommentFulfillmentObligation)
        .where(
            CommentFulfillmentObligation.task_id == task.id,
            CommentFulfillmentObligation.status.in_(OPEN_OBLIGATION_STATUSES),
            CommentFulfillmentObligation.current_action_id.is_(None),
        )
    ).all()
    closed = 0
    for obligation in rows:
        message = session.get(ChannelMessage, obligation.channel_message_id)
        if message:
            source_window = comment_source_window(task, message)
            if source_window is None:
                continue
            _period_start, deadline = source_window
            if deadline <= now_val:
                obligation.status = "closed_expired"
                closed += 1
    if closed:
        stats = dict(task.stats or {})
        stats["window_expired_settled_count"] = int(stats.get("window_expired_settled_count") or 0) + closed
        task.stats = stats
        session.flush()
    return closed


__all__ = ["bind_comment_obligation", "clean_expired_comment_obligations", "freeze_comment_obligations"]
