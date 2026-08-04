from __future__ import annotations

from sqlalchemy import and_, case, func, or_, select

from app.models import Action, GroupBotAdmission, Task, TaskGroupBotAdmission


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
    ready_task_admission = (
        select(TaskGroupBotAdmission.id)
        .where(
            TaskGroupBotAdmission.task_id == Action.task_id,
            TaskGroupBotAdmission.target_group_id == group_id,
            TaskGroupBotAdmission.account_id == Action.account_id,
            TaskGroupBotAdmission.state == "ready",
        )
        .exists()
    )
    admission_ready = or_(
        and_(Task.fulfillment_contract_version == "fact_first_v3", ready_task_admission),
        and_(
            or_(
                Task.fulfillment_contract_version != "fact_first_v3",
                Task.fulfillment_contract_version.is_(None),
            ),
            ready_admission,
        ),
    )
    admission_disabled = (
        Task.type_config["group_bot_admission_required"].as_boolean().is_(False)
    )
    waiting_send = (
        (Action.task_type == "group_ai_chat")
        & (Action.action_type == "send_message")
        & ~admission_disabled
        & ~admission_ready
    )
    return case((waiting_send, 1), else_=0)


__all__ = ["CLAIMABLE_ADMISSION_STATES", "group_bot_admission_claim_rank"]
