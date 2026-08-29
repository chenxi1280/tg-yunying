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
    reply_targets: list[dict | None],
    *,
    rule_version: RuleSetVersion,
    reply_min_required: int,
    first_ordinal: int = 1,
) -> list[CommentFulfillmentObligation]:
    existing = _message_obligations(session, task, message.id)
    reusable: list[CommentFulfillmentObligation] = []
    if existing:
        _release_terminal_bindings(session, existing)
        available = [
            item
            for item in existing
            if item.status in OPEN_OBLIGATION_STATUSES
            and item.current_action_id is None
        ]
        if available:
            reusable, reply_targets = _reuse_available_obligations(
                available,
                reply_targets,
            )
            if not reply_targets:
                return reusable
    revision = int(task.config_revision or 1)
    contract = _create_comment_contract(
        session,
        task,
        message,
        reply_targets,
        rule_version=rule_version,
        revision=revision,
        reply_min_required=reply_min_required,
    )
    rows = _new_obligations(
        task,
        message,
        contract,
        revision=revision,
        first_ordinal=first_ordinal,
        reply_targets=reply_targets,
    )
    session.add_all(rows)
    session.flush()
    return [*reusable, *rows]


def _new_obligations(
    task: Task,
    message: ChannelMessage,
    contract: ContentMixContract,
    *,
    revision: int,
    first_ordinal: int,
    reply_targets: list[dict | None],
) -> list[CommentFulfillmentObligation]:
    return [
        _new_obligation(
            task,
            message,
            contract,
            revision=revision,
            ordinal=index,
            reply_target=target,
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
    targets: list[dict | None],
    *,
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
) -> CommentFulfillmentObligation:
    snapshot = dict(reply_target or {})
    reply_id = _reply_target_id(snapshot)
    return CommentFulfillmentObligation(
        tenant_id=task.tenant_id,
        task_id=task.id,
        channel_message_id=message.id,
        comment_plan_revision=revision,
        target_ordinal=ordinal,
        content_mix_contract_id=contract.id,
        relation_kind="reply" if reply_id else "direct",
        reply_to_message_id=reply_id,
        reply_target_snapshot=snapshot,
        status="open",
    )


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
    from .source_pacing import rolling_source_window
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
            _period_start, deadline = rolling_source_window(task, message.created_at)
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
