from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import OperationTarget, Task

from ..account_pool import select_task_accounts
from ..channel_comment_plan_contract import grounding_plan_enabled
from ..channel_membership import channel_member_accounts
from ..comment_account_profiles import comment_account_profile_ready
from ..daily_ledgers import ensure_task_day_ledger
from ..engagement_participation import (
    ensure_daily_participation_plan,
    selected_accounts_for_plan,
)
from ..engagement_planning_admission import ensure_planning_admission_snapshot
from .common import quantity_jitter_bounds


PROFILE_ERROR = "评论账号资料未初始化，请先在账号中心批量初始化中文昵称、username 和头像"


@dataclass(frozen=True)
class CommentAccountSetup:
    ledger: object | None
    participation_plan: object | None
    admission_snapshot: object | None
    policy_accounts: list
    accounts: list


def prepare_comment_accounts(
    session: Session,
    task: Task,
    channel: OperationTarget,
    *,
    config: dict,
) -> CommentAccountSetup:
    ledger, plan = _daily_participation(session, task)
    candidates = _candidate_accounts(
        session, task, config=config, participation_plan=plan
    )
    if plan is not None:
        admission = ensure_planning_admission_snapshot(
            session,
            task,
            plan,
            planning_horizon=f"task_day:{ledger.obligation_local_date.isoformat()}",
            target=channel,
        )
        admissible_ids = {int(item) for item in admission.admissible_account_ids or []}
        executable = [account for account in candidates if account.id in admissible_ids]
        accounts = _profile_ready_accounts(task, executable)
        if not accounts:
            task.last_error = PROFILE_ERROR if executable else "planning_admission_blocked"
        return CommentAccountSetup(ledger, plan, admission, candidates, accounts)
    grounding_v1 = grounding_plan_enabled(task)
    ready = candidates if grounding_v1 else channel_member_accounts(
        session, task, channel, candidates, require_send=True
    )
    accounts = _profile_ready_accounts(task, ready)
    if not accounts:
        task.last_error = PROFILE_ERROR if ready else "没有可用账号，等待账号恢复后继续执行"
    return CommentAccountSetup(ledger, plan, None, accounts, accounts)


def _candidate_accounts(
    session: Session,
    task: Task,
    *,
    config: dict,
    participation_plan: object | None,
) -> list:
    if participation_plan is not None:
        return selected_accounts_for_plan(session, task, participation_plan)
    grounding_v1 = grounding_plan_enabled(task)
    target = int(config.get("target_comments_per_message") or 1)
    _lower, maximum = quantity_jitter_bounds(
        target, float(config.get("comment_count_jitter") or 0)
    )
    is_all_mode = (task.account_config or {}).get("selection_mode") == "all"
    scan_limit = max(
        maximum, int((task.account_config or {}).get("max_concurrent") or maximum)
    )
    return select_task_accounts(
        session,
        task.tenant_id,
        task.account_config or {},
        limit=None if is_all_mode else scan_limit,
        enforce_max_concurrent=False,
        enforce_capacity=not grounding_v1,
        scan_all_candidates=grounding_v1 or is_all_mode,
        daily_coverage_task_id=task.id,
        daily_coverage_action_types=("post_comment",),
    )


def _daily_participation(session: Session, task: Task) -> tuple[object, object] | tuple[None, None]:
    if (task.type_config or {}).get("engagement_contract_version") != "unified_engagement_v1":
        return None, None
    ledger = ensure_task_day_ledger(session, task)
    return ledger, ensure_daily_participation_plan(session, task, ledger)


def _profile_ready_accounts(task: Task, accounts: list) -> list:
    ready = [account for account in accounts if comment_account_profile_ready(account)]
    stats = dict(task.stats or {})
    if len(ready) != len(accounts):
        stats["comment_profile_blocked_account_count"] = len(accounts) - len(ready)
        stats["comment_profile_ready_account_count"] = len(ready)
    else:
        stats.pop("comment_profile_blocked_account_count", None)
        stats.pop("comment_profile_ready_account_count", None)
    if ready and task.last_error == PROFILE_ERROR:
        task.last_error = ""
    task.stats = stats
    return ready
