"""Indexes for the AI surface baseline and the complete group observation window."""
from sqlalchemy import Index, func

from .groups import GroupContextMessage
from .task_center import Action


Index(
    "ix_actions_ai_surface_history",
    Action.tenant_id,
    Action.task_type,
    Action.payload["surface_scope_key"].as_string(),
)
Index(
    "ix_group_context_messages_observation_recent",
    GroupContextMessage.tenant_id,
    GroupContextMessage.group_id,
    func.coalesce(GroupContextMessage.sent_at, GroupContextMessage.created_at).desc(),
    GroupContextMessage.id.desc(),
)
