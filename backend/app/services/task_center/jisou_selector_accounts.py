from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Action, Task, TgAccount
from app.timezone import as_beijing

from .search_join_facts import has_confirmed_click_fact


JISOU_USERNAME = "jisou"
SOURCE_ACTION_TYPE = "search_join"
LOOKBACK_WINDOW = timedelta(hours=12)
TARGET_CLICK_OUTCOME = "target_click"

# PRD §2.19.3 12 小时排除规则：以下错误码触发后账号 12 小时排除，
# jisou_group_selector_missing 不自动排除（单独评估协议样本是否过期）。
EXCLUSION_ERROR_CODES = frozenset(
    {
        "jisou_session_state_deviated",
        "jisou_hot_list_page",
        "jisou_image_verification_failed",
    }
)


@dataclass(frozen=True)
class JisouSelectorCandidates:
    accounts: tuple[TgAccount, ...]
    excluded_count: int


def select_jisou_selector_candidates(
    session: Session,
    task: Task,
    accounts: list[TgAccount],
    *,
    bot_username: str,
    now_value: datetime,
) -> JisouSelectorCandidates:
    if _normalized_bot_username(bot_username) != JISOU_USERNAME:
        return JisouSelectorCandidates(tuple(accounts), 0)
    outcomes = _latest_account_outcomes(session, task, now_value)
    excluded_ids = {
        account_id for account_id, outcome in outcomes.items()
        if outcome != TARGET_CLICK_OUTCOME and _outcome_excluded(outcome)
    }
    eligible = tuple(account for account in accounts if account.id not in excluded_ids)
    verified_ids = {
        account_id for account_id, outcome in outcomes.items()
        if outcome == TARGET_CLICK_OUTCOME
    }
    ordered = tuple(sorted(eligible, key=lambda account: account.id not in verified_ids))
    return JisouSelectorCandidates(ordered, len(accounts) - len(eligible))


def _outcome_excluded(outcome: str) -> bool:
    return outcome in EXCLUSION_ERROR_CODES


def _latest_account_outcomes(session: Session, task: Task, now_value: datetime) -> dict[int, str]:
    cutoff = now_value - LOOKBACK_WINDOW
    actions = session.scalars(
        select(Action)
        .where(
            Action.tenant_id == task.tenant_id,
            Action.action_type == SOURCE_ACTION_TYPE,
            or_(
                Action.executed_at >= cutoff,
                Action.scheduled_at >= cutoff,
                Action.created_at >= cutoff,
            ),
        )
        .order_by(
            func.coalesce(
                Action.executed_at,
                Action.scheduled_at,
                Action.created_at,
            ).asc(),
            Action.id.asc(),
        )
    )
    outcomes: dict[int, str] = {}
    for action in actions:
        observed_at = as_beijing(action.executed_at or action.scheduled_at or action.created_at)
        outcome = _action_outcome(action)
        if (
            _action_bot_username(action) == JISOU_USERNAME
            and action.account_id is not None
            and observed_at is not None
            and observed_at >= cutoff
            and outcome
        ):
            outcomes[int(action.account_id)] = outcome
    return outcomes


def _action_outcome(action: Action) -> str:
    result = action.result if isinstance(action.result, dict) else {}
    if has_confirmed_click_fact(result):
        return TARGET_CLICK_OUTCOME
    error_code = str(result.get("error_code") or "")
    if (
        error_code == "jisou_hot_list_page"
        and not result.get("jisou_bootstrap_kind")
    ):
        return ""
    if error_code in EXCLUSION_ERROR_CODES or error_code == "jisou_group_selector_missing":
        return error_code
    return ""


def _normalized_bot_username(value: str) -> str:
    return value.strip().lower().lstrip("@")


def _action_bot_username(action: Action) -> str:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return _normalized_bot_username(str(payload.get("bot_username") or ""))


__all__ = ["JisouSelectorCandidates", "select_jisou_selector_candidates"]
