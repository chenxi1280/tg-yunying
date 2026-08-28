"""Read-only snapshot of group rescue configuration and recent task state."""

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Action, Task, TaskMembershipAdmissionItem, Tenant, TgAccount


def main() -> None:
    with SessionLocal() as session:
        print("=" * 80)
        print("【天津一品楼 群聊救援 (Group Rescue) 机制深度诊断】")
        print("=" * 80)
        _print_tenant_settings(session)
        _print_task_state(session)


def _print_tenant_settings(session) -> None:
    for tenant in session.scalars(select(Tenant)):
        admin = (
            session.get(TgAccount, tenant.group_rescue_admin_account_id)
            if tenant.group_rescue_admin_account_id
            else None
        )
        print(f"Tenant ID: {tenant.id} | Name: {tenant.name}")
        print(f"  group_rescue_enabled: {tenant.group_rescue_enabled}")
        print(
            "  group_rescue_admin_account_id: "
            f"{tenant.group_rescue_admin_account_id} "
            f"({admin.display_name if admin else '未配置/不存在'})"
        )
        if admin:
            print(f"  Admin Account Status: {admin.status}, Phone: {admin.phone_masked}")


def _print_task_state(session) -> None:
    task = session.scalar(
        select(Task)
        .where(Task.name.like("%天津一品楼%"))
        .where(Task.deleted_at.is_(None))
    )
    if task is None:
        print("未找到天津一品楼任务")
        return
    items = list(
        session.scalars(
            select(TaskMembershipAdmissionItem).where(
                TaskMembershipAdmissionItem.task_id == task.id
            )
        )
    )
    print(f"\nTask: {task.name} (ID: {task.id})")
    print(f"总准入条目数: {len(items)}")
    rescued = [item for item in items if item.rescue_action_id or item.rescue_status]
    print(f"触发了救援动作的条目数: {len(rescued)}")
    _print_items(items[:10])
    _print_actions(session, task.id)


def _print_items(items) -> None:
    for item in items:
        print(
            f"  Item ID {item.id} | Account ID {item.account_id}: "
            f"Phase={item.phase}, FailureType={item.failure_type}"
        )
        print(
            f"    RescueActionID={item.rescue_action_id}, "
            f"RescueStatus={item.rescue_status}, "
            f"RescueDetail={item.rescue_failure_detail}"
        )


def _print_actions(session, task_id: str) -> None:
    actions = list(
        session.scalars(
            select(Action).where(
                Action.task_id == task_id,
                Action.action_type.in_(["invite_group_account", "invite_group_bot"]),
            )
        )
    )
    print(f"\n任务下群聊救援 Action 记录数: {len(actions)}")
    for action in actions[:10]:
        print(
            f"  Action ID {action.id[:8]}... | Status={action.status} | "
            f"AccountID={action.account_id} | Payload={action.payload} | "
            f"Result={action.result}"
        )


if __name__ == "__main__":
    main()
