from __future__ import annotations

from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    AccountStatus,
    Action,
    GroupBotAdmission,
    Task,
    Tenant,
    TgAccount,
)
from app.services._common import _now
from app.services.task_center.group_bot_claim_priority import (
    group_bot_admission_claim_rank,
)


def test_postgres_orders_probe_send_before_waiting_send() -> None:
    with SessionLocal() as session:
        session.add(Tenant(id=901, name="PostgreSQL 排序"))
        session.flush()
        task = Task(
            id="postgres-admission-priority",
            tenant_id=901,
            name="PostgreSQL 准入排序",
            type="group_ai_chat",
            status="running",
            type_config={
                "target_group_id": 907,
                "group_bot_admission_required": True,
            },
        )
        session.add(task)
        session.flush()
        session.add_all([
            _account(911, tenant_id=901),
            _account(912, tenant_id=901),
        ])
        session.flush()
        session.add_all([
            GroupBotAdmission(
                tenant_id=901,
                group_id=907,
                account_id=911,
                state="group_bot_policy_unresolved",
            ),
            GroupBotAdmission(
                tenant_id=901,
                group_id=907,
                account_id=912,
                state="post_follow_visibility_probe",
            ),
            _send_action("waiting-send", task, account_id=911),
            _send_action("probe-send", task, account_id=912),
        ])
        session.flush()

        action_ids = session.scalars(
            select(Action.id)
            .join(Task, Task.id == Action.task_id)
            .where(Action.task_id == task.id)
            .order_by(group_bot_admission_claim_rank(), Action.id)
        ).all()

        assert action_ids == ["probe-send", "waiting-send"]


def _send_action(action_id: str, task: Task, *, account_id: int) -> Action:
    return Action(
        id=action_id,
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        account_id=account_id,
        status="pending",
        scheduled_at=_now(),
        payload={"group_id": 907},
    )


def _account(account_id: int, *, tenant_id: int) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=tenant_id,
        display_name=f"账号{account_id}",
        phone_masked=str(account_id),
        status=AccountStatus.ACTIVE.value,
    )
