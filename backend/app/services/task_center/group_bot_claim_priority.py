from __future__ import annotations

from sqlalchemy import case, func, select

from app.models import Action, GroupBotAdmission, Task


CLAIMABLE_ADMISSION_STATES = (
    "group_bot_admission_ready",
    "post_follow_visibility_probe",
)


def group_bot_admission_claim_rank():
    """Keep admission-waiting sends behind accounts that may send now."""
    group_id = func.coalesce(
        Action.payload["group_id"].as_integer(),
        Task.type_config["target_group_id"].as_integer(),
        0,
    )
    ready_admission = (
        select(GroupBotAdmission.id)
        .where(
            GroupBotAdmission.tenant_id == Action.tenant_id,
            GroupBotAdmission.group_id == group_id,
            GroupBotAdmission.account_id == Action.account_id,
            GroupBotAdmission.state.in_(CLAIMABLE_ADMISSION_STATES),
        )
        .exists()
    )
    admission_disabled = (
        Task.type_config["group_bot_admission_required"].as_boolean().is_(False)
    )
    waiting_send = (
        (Action.task_type == "group_ai_chat")
        & (Action.action_type == "send_message")
        & ~admission_disabled
        & ~ready_admission
    )
    return case((waiting_send, 1), else_=0)


__all__ = ["CLAIMABLE_ADMISSION_STATES", "group_bot_admission_claim_rank"]
