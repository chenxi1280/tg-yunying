"""Attributed negative feedback from real listener and visibility facts."""
from datetime import datetime

from sqlalchemy import select

from app.models import Action, ExecutionAttempt, FulfillmentRemoteFact, Task
from app.timezone import as_beijing

from .engagement_activity_scope import action_activity_scope
from .negative_outcome_circuit import (
    detect_ai_suspicion_in_text, record_negative_outcome, recover_circuit_from_visibility,
)

INTERACTIVE_ROUTES = frozenset({"group_ai_chat", "channel_comment"})
DIRECT_COMPLAINTS = ("别刷", "别再", "不要再", "停止", "答非所问", "胡说", "乱说", "stop spamming")
PREMATURE_COMPLAINTS = ("我还没说完", "别抢答", "不要抢答")


def observe_human_negative_reply(session, task, *, peer_id, payload):
    if task.type not in INTERACTIVE_ROUTES or payload.get("sender_is_bot"):
        return
    text = str(payload.get("content") or "").lower()
    event_type = _explicit_complaint(text)
    parent = str(payload.get("reply_to_message_id") or "")
    remote_id = str(payload.get("source_message_id") or "")
    if not event_type or not parent.isdigit() or not remote_id.isdigit() or not payload.get("sent_at"):
        return
    actions = _confirmed_parent_actions(session, task, peer_id=peer_id, parent=parent)
    if len(actions) != 1:
        return  # Ambiguous attribution is not permission to quarantine an account.
    action = actions[0]
    record_negative_outcome(
        session, tenant_id=task.tenant_id, route=task.type, peer_id=peer_id,
        account_id=action.account_id, event_type=event_type,
        event_id=f"human_reply:{peer_id}:{remote_id}",
        observed_at=as_beijing(datetime.fromisoformat(str(payload["sent_at"]))),
        evidence={"action_id": action.id, "parent_remote_id": parent, "human_remote_id": remote_id},
    )


def _explicit_complaint(text):
    if any(word in text for word in PREMATURE_COMPLAINTS):
        return "premature_answer"
    if detect_ai_suspicion_in_text(text) and any(word in text for word in DIRECT_COMPLAINTS):
        return "ai_suspicion"
    return ""


def _confirmed_parent_actions(session, task, *, peer_id, parent):
    actions = session.scalars(select(Action).join(
        ExecutionAttempt, ExecutionAttempt.action_id == Action.id,
    ).join(FulfillmentRemoteFact, FulfillmentRemoteFact.attempt_id == ExecutionAttempt.id).where(
        Action.tenant_id == task.tenant_id, Action.task_type == task.type,
        Action.account_id.is_not(None), ExecutionAttempt.remote_message_id == parent,
        FulfillmentRemoteFact.tenant_id == task.tenant_id,
        FulfillmentRemoteFact.action_id == Action.id,
        FulfillmentRemoteFact.fact_kind == "remote_message_observed",
    ).distinct())
    return [action for action in actions
            if action_activity_scope(session, action).canonical_peer_id == peer_id]


def observe_visibility_outcome(session, action, observation):
    task = session.get(Task, action.task_id)
    if not task or task.type not in INTERACTIVE_ROUTES or not action.account_id:
        return
    if (task.type_config or {}).get("engagement_contract_version") != "unified_engagement_v1":
        return
    if not observation.target_peer or not observation.remote_message_id:
        return
    scope = dict(tenant_id=action.tenant_id, route=task.type,
                 peer_id=observation.target_peer, account_id=action.account_id)
    if observation.state == "visible_confirmed":
        recover_circuit_from_visibility(session, **scope, observed_at=observation.checked_at)
    elif observation.terminal_reason == "post_send_intercepted":
        record_negative_outcome(
            session, **scope, event_type="bot_intercept",
            event_id=f"visibility:{observation.target_peer}:{observation.remote_message_id}",
            observed_at=observation.checked_at,
            evidence={"action_id": action.id, "attempt_id": observation.attempt_id,
                      "observation_id": observation.id,
                      "remote_message_id": observation.remote_message_id},
        )
