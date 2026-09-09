"""Persist account/Task claim pacing decisions without changing ownership."""
from dataclasses import dataclass
from datetime import datetime

from app.models import AccountPacingReservation, Action
from .account_pacing_policy import wall_time as _wall
from .account_pacing_window import effective_claim_at


@dataclass(frozen=True)
class PacingClaimDecision:
    allowed: bool
    effective_claim_at: datetime | None = None
    reason_code: str = ""



def _settle_claim_pacing(
    action: Action,
    reservation: AccountPacingReservation,
    *,
    now_value: datetime,
    desired_at: datetime,
    not_before: datetime | None,
    group_conflict: bool,
) -> PacingClaimDecision:
    effective_at = effective_claim_at(desired_at, not_before)
    if reservation.source_deadline_at and not _before_deadline(
        effective_at, reservation.source_deadline_at,
    ):
        return PacingClaimDecision(False, effective_at, "pacing_claim_deadline_exceeded")
    if effective_at <= _wall(now_value):
        _sync_claim_time(action, reservation, effective_at)
        return PacingClaimDecision(True, effective_at)
    reason = "group_send_pacing_conflict" if group_conflict else "account_timeline_conflict"
    _defer_action_claim(action, reservation, effective_at, reason_code=reason)
    return PacingClaimDecision(False, effective_at, reason)


def _defer_action_claim(
    action: Action,
    reservation: AccountPacingReservation,
    effective_at: datetime,
    *,
    reason_code: str = "account_timeline_conflict",
) -> None:
    action.scheduled_at = effective_at
    action.effective_claim_at = effective_at
    if action.release_not_before_at is not None:
        action.release_not_before_at = max(
            _wall(action.release_not_before_at),
            effective_at,
        )
    else:
        action.release_not_before_at = effective_at
    action.action_version = int(action.action_version or 1) + 1
    action.result = {
        **(action.result or {}),
        "claim_pacing_deferred": {
            "reason_code": reason_code,
            "effective_claim_at": effective_at.isoformat(),
        },
    }
    reservation.effective_claim_at = effective_at
    reservation.version = int(reservation.version or 1) + 1


def _sync_claim_time(
    action: Action,
    reservation: AccountPacingReservation,
    effective_at: datetime,
) -> None:
    # claim 放行即占位：scheduled_at 锚定到 claim 时刻，使 claiming 在途点
    # 进入时间线窗口（start_at = now - gap）。不更新会保留过期老值，同批
    # 后续 claim 与并发 final gate 都看不见本条在途 → 同秒批量挤发
    # （2026-08-17 部署后线上实测 min gap 0.12s）。
    action.scheduled_at = effective_at
    action.effective_claim_at = effective_at
    if reservation.effective_claim_at == effective_at:
        return
    reservation.effective_claim_at = effective_at
    reservation.version = int(reservation.version or 1) + 1


def _before_deadline(value: datetime, deadline: datetime) -> bool:
    normalized = _wall(deadline)
    return normalized is not None and value < normalized

