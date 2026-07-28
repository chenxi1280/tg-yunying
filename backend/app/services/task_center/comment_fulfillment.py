from __future__ import annotations

import json

from sqlalchemy import select
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
    if existing:
        _release_terminal_bindings(session, existing)
        return [
            item
            for item in existing
            if item.status in OPEN_OBLIGATION_STATUSES
            and item.current_action_id is None
        ]
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
    rows = [
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
    session.add_all(rows)
    session.flush()
    return rows


def _release_terminal_bindings(
    session: Session,
    obligations: list[CommentFulfillmentObligation],
) -> None:
    for obligation in obligations:
        if not obligation.current_action_id:
            continue
        action = session.get(Action, obligation.current_action_id)
        if action is None or action.status not in {
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
    contract = ContentMixContract(
        tenant_id=task.tenant_id,
        content_mix_scope_key=f"comment:{task.id}:{message.id}:{revision}",
        content_contract_version=revision,
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
    reply_id = int(
        snapshot.get("message_id")
        or snapshot.get("comment_message_id")
        or 0
    ) or None
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


__all__ = ["bind_comment_obligation", "freeze_comment_obligations"]
