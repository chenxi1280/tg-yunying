from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import GroupContextMessage, OperationTarget, Task, TgGroup


AI_CONTEXT_TASK_STATES = ("pending", "running", "paused")


def ai_context_tracking_enabled(session: Session, group: TgGroup) -> bool:
    return session.scalar(
        select(Task.id)
        .outerjoin(
            OperationTarget,
            OperationTarget.id
            == Task.type_config["target_operation_target_id"].as_integer(),
        )
        .where(
            Task.tenant_id == group.tenant_id,
            Task.type == "group_ai_chat",
            Task.status.in_(AI_CONTEXT_TASK_STATES),
            Task.deleted_at.is_(None),
            Task.type_config["ai_content_route_v2_enabled"].as_boolean().is_(True),
            or_(
                Task.type_config["target_group_id"].as_integer() == group.id,
                and_(
                    OperationTarget.tenant_id == group.tenant_id,
                    OperationTarget.target_type == "group",
                    OperationTarget.tg_peer_id == group.tg_peer_id,
                ),
            ),
        ).limit(1)
    ) is not None


def record_ai_context_message(
    session: Session,
    group: TgGroup,
    message: GroupContextMessage,
) -> None:
    if message.is_bot:
        return
    from app.services.task_center.ai_content_runtime import (
        bump_context_revision,
        context_message_hash,
    )

    bump_context_revision(
        session,
        tenant_id=group.tenant_id,
        scope_type="group",
        scope_id=str(group.id),
        snapshot_hash=context_message_hash(
            remote_message_id=message.remote_message_id,
            content=message.content,
        ),
        human_message_id=message.remote_message_id,
    )


__all__ = ["ai_context_tracking_enabled", "record_ai_context_message"]
