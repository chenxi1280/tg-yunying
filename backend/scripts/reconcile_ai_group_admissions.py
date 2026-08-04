from __future__ import annotations

import argparse
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    Action,
    GroupBotAdmission,
    GroupBotRequiredChannelFollow,
    GroupContextMessage,
    Task,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
    TgGroupAccount,
)
from app.services.task_center.group_bot_admission import (
    active_policy,
    create_policy,
    ensure_admission_after_join,
    plan_confirmation_button_action,
    plan_required_channel_follow_actions,
    reconcile_unresolved_with_not_required,
)
from app.services.task_center.group_bot_observation import (
    latest_persisted_group_cursor,
    restart_admission_observation,
)


LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
def _tasks(session, names: tuple[str, ...]) -> list[Task]:
    return list(
        session.scalars(
            select(Task).where(
                Task.type == "group_ai_chat",
                Task.name.in_(names),
                Task.status == "running",
                Task.deleted_at.is_(None),
            )
        )
    )


def _group_id(task: Task) -> int:
    return int((task.type_config or {}).get("target_group_id") or 0)


def _state_counts(session, group_id: int) -> dict[str, int]:
    rows = session.execute(
        select(GroupBotAdmission.state, func.count(GroupBotAdmission.id))
        .where(GroupBotAdmission.group_id == group_id)
        .group_by(GroupBotAdmission.state)
    )
    return {str(state): int(count) for state, count in rows}


def _missing_coverage_accounts(
    session,
    task: Task,
    group_id: int,
    local_date,
) -> list[int]:
    admission_accounts = select(GroupBotAdmission.account_id).where(
        GroupBotAdmission.tenant_id == task.tenant_id,
        GroupBotAdmission.group_id == group_id,
    )
    statement = (
        select(TaskAccountDailyCoverage.account_id)
        .join(
            TgGroupAccount,
            (TgGroupAccount.group_id == group_id)
            & (
                TgGroupAccount.account_id
                == TaskAccountDailyCoverage.account_id
            ),
        )
        .where(
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.coverage_date == local_date,
            TaskAccountDailyCoverage.confirmed_count
            < TaskAccountDailyCoverage.target_count,
            TgGroupAccount.can_send.is_(True),
            TaskAccountDailyCoverage.account_id.not_in(admission_accounts),
        )
        .order_by(TaskAccountDailyCoverage.account_id)
    )
    return [int(value) for value in session.scalars(statement)]


def _membership_action_id(
    session,
    task_id: str,
    account_id: int,
) -> str:
    value = session.scalar(
        select(TaskMembershipAdmissionItem.membership_action_id).where(
            TaskMembershipAdmissionItem.task_id == task_id,
            TaskMembershipAdmissionItem.account_id == account_id,
        )
    )
    return str(value or "")


def _create_missing_admissions(
    session,
    task: Task,
    group_id: int,
    account_ids: list[int],
    cursor: str,
) -> int:
    count = 0
    for account_id in account_ids:
        row = ensure_admission_after_join(
            session,
            tenant_id=task.tenant_id,
            group_id=group_id,
            account_id=account_id,
            membership_action_id=_membership_action_id(
                session, task.id, account_id,
            ),
            join_start_cursor=cursor,
        )
        row.evidence_ref = (
            f"production-recovery:legacy-current-waterline:{cursor}"
        )
        count += 1
    return count


def _restart_stale_admissions(
    session,
    *,
    tenant_id: int,
    group_id: int,
) -> int:
    rows = list(
        session.scalars(
            select(GroupBotAdmission).where(
                GroupBotAdmission.tenant_id == tenant_id,
                GroupBotAdmission.group_id == group_id,
                GroupBotAdmission.state == "observation_stale",
            )
        )
    )
    for row in rows:
        restart_admission_observation(
            session,
            admission=row,
            expected_admission_version=int(row.admission_version or 1),
            reason="生产恢复：使用当前持久化 listener 水位重启观察",
            evidence_ref="production-recovery:listener-waterline",
        )
    return len(rows)


def _context_for_admission(
    session,
    admission: GroupBotAdmission,
) -> GroupContextMessage | None:
    source_id = str(admission.source_message_id or "")
    if not source_id:
        return None
    return session.scalar(
        select(GroupContextMessage).where(
            GroupContextMessage.tenant_id == admission.tenant_id,
            GroupContextMessage.group_id == admission.group_id,
            GroupContextMessage.remote_message_id == source_id,
        )
    )


def _release_terminal_follow_binding(
    session,
    row: GroupBotRequiredChannelFollow,
) -> bool:
    if not row.action_id:
        return False
    action = session.get(Action, row.action_id)
    if action is None:
        row.action_id = ""
        return True
    return False


def _replan_admission_actions(
    session,
    task: Task,
    group_id: int,
) -> dict[str, int]:
    counts = {"follow_bindings_released": 0, "follow_actions": 0, "confirmation_actions": 0}
    admissions = list(
        session.scalars(
            select(GroupBotAdmission).where(
                GroupBotAdmission.tenant_id == task.tenant_id,
                GroupBotAdmission.group_id == group_id,
                GroupBotAdmission.state.in_(
                    {
                        "required_channel_follow_pending",
                        "awaiting_group_bot_confirmation",
                    }
                ),
            )
        )
    )
    for admission in admissions:
        context = _context_for_admission(session, admission)
        if context is None:
            continue
        counts["follow_bindings_released"] += _release_follow_bindings(
            session, admission,
        )
        counts["follow_actions"] += len(
            plan_required_channel_follow_actions(
                session,
                admission=admission,
                task_id=task.id,
                source_message_id=str(context.remote_message_id),
                control_buttons=context.control_buttons or [],
                prompt_text=context.content,
            )
        )
        action = plan_confirmation_button_action(
            session,
            admission=admission,
            task_id=task.id,
            source_message_id=str(context.remote_message_id),
            control_buttons=context.control_buttons or [],
        )
        counts["confirmation_actions"] += int(action is not None)
    return counts


def _release_follow_bindings(
    session,
    admission: GroupBotAdmission,
) -> int:
    rows = session.scalars(
        select(GroupBotRequiredChannelFollow).where(
            GroupBotRequiredChannelFollow.admission_id == admission.id,
            GroupBotRequiredChannelFollow.status == "pending",
        )
    )
    return sum(
        int(_release_terminal_follow_binding(session, row))
        for row in rows
    )


def _ensure_not_required_policy(
    session,
    task: Task,
    group_id: int,
) -> tuple[int, int]:
    current = active_policy(
        session,
        tenant_id=task.tenant_id,
        group_id=group_id,
        completion_policy="not_required",
    )
    created = 0
    if current is None:
        unresolved = _state_counts(session, group_id).get(
            "group_bot_policy_unresolved", 0,
        )
        if unresolved <= 0:
            raise RuntimeError(
                f"{task.name} has no closed unresolved observation evidence"
            )
        current = create_policy(
            session,
            tenant_id=task.tenant_id,
            group_id=group_id,
            completion_policy="not_required",
            reason="生产恢复：连续观察已闭合且未发现群管准入规则",
            evidence_ref=(
                f"production-observation:group={group_id};"
                f"policy_unresolved={unresolved}"
            ),
            created_by="user-authorized-production-recovery",
        )
        created = 1
    reconciled = reconcile_unresolved_with_not_required(
        session,
        tenant_id=task.tenant_id,
        group_id=group_id,
    )
    return created, reconciled


def _task_preview(session, task: Task, local_date) -> dict:
    group_id = _group_id(task)
    return {
        "task_id": task.id,
        "task_name": task.name,
        "group_id": group_id,
        "latest_cursor": latest_persisted_group_cursor(
            session, group_id=group_id,
        ),
        "missing_coverage_admissions": len(
            _missing_coverage_accounts(
                session, task, group_id, local_date,
            )
        ),
        "state_counts": _state_counts(session, group_id),
    }


def reconcile(
    task_names: tuple[str, ...],
    *,
    not_required_tasks: frozenset[str],
    apply: bool,
) -> dict:
    local_date = datetime.now(LOCAL_TIMEZONE).date()
    with SessionLocal() as session:
        tasks = _tasks(session, task_names)
        previews = [_task_preview(session, task, local_date) for task in tasks]
        result = {"mode": "apply" if apply else "preview", "tasks": previews}
        if not apply:
            session.rollback()
            return result
        applied = []
        for task in tasks:
            applied.append(
                _apply_task(
                    session,
                    task,
                    local_date,
                    not_required=task.name in not_required_tasks,
                )
            )
        session.commit()
        result["applied"] = applied
        return result


def _apply_task(
    session,
    task: Task,
    local_date,
    *,
    not_required: bool,
) -> dict:
    group_id = _group_id(task)
    policy_created = policy_reconciled = 0
    if not_required:
        policy_created, policy_reconciled = _ensure_not_required_policy(
            session, task, group_id,
        )
    cursor = latest_persisted_group_cursor(session, group_id=group_id)
    missing = _missing_coverage_accounts(
        session, task, group_id, local_date,
    )
    created = _create_missing_admissions(
        session, task, group_id, missing, cursor,
    )
    restarted = _restart_stale_admissions(
        session,
        tenant_id=task.tenant_id,
        group_id=group_id,
    )
    actions = _replan_admission_actions(session, task, group_id)
    return {
        "task_name": task.name,
        "policy_created": policy_created,
        "policy_reconciled": policy_reconciled,
        "missing_admissions_created": created,
        "stale_observations_restarted": restarted,
        **actions,
        "state_counts_after": _state_counts(session, group_id),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile blocked AI group admissions from audited facts."
    )
    parser.add_argument("--task-name", action="append", required=True)
    parser.add_argument("--not-required-task", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            reconcile(
                tuple(args.task_name),
                not_required_tasks=frozenset(args.not_required_task),
                apply=bool(args.apply),
            ),
            ensure_ascii=False,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
