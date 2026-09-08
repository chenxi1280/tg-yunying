"""Shared identity and validity rules for confirmed operation statistics."""
from types import MappingProxyType

from sqlalchemy import and_, func, or_

from app.models import Action, ExecutionAttempt, FulfillmentRemoteFact


WINDOW_HOURS = 72
SUCCESS_KINDS = MappingProxyType({
    "group_ai_chat": ("send_message", "remote_message_observed", "确认发送"),
    "channel_comment": ("post_comment", "remote_message_observed", "确认评论"),
    "channel_like": ("like_message", "reaction_observed", "确认点赞操作"),
    "channel_view": ("view_message", "view_observed", "确认浏览操作"),
})


def valid_success_predicates() -> tuple:
    fact = FulfillmentRemoteFact
    return (
        fact.tenant_id == Action.tenant_id,
        fact.task_id == Action.task_id,
        fact.task_type == Action.task_type,
        fact.action_id == Action.id,
        ExecutionAttempt.tenant_id == fact.tenant_id,
        ExecutionAttempt.action_id == Action.id,
        fact.outcome["action_status"].as_string() == "success",
        fact.outcome["attempt_status"].as_string() == "success",
        or_(*(and_(fact.task_type == task_type, Action.action_type == mutation,
                   fact.mutation_kind == mutation, fact.fact_kind == kind)
              for task_type, (mutation, kind, _label) in SUCCESS_KINDS.items())),
        or_(fact.fact_kind != "remote_message_observed",
            func.length(func.trim(fact.outcome["remote_message_id"].as_string())) > 0),
    )
