from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, FulfillmentRemoteFact
from app.timezone import BEIJING_TZ

from .account_pacing_guard import _OPEN_GUARD_STATUSES
from .pacing import PACING_CONTRACT_VERSION


PACING_SUMMARY_SAMPLE_LIMIT = 5000
DUE_TO_LATE_GRACE_SECONDS = 300
_FIVE_MINUTE_WINDOW_SECONDS = 300
_TERMINAL_MISSED_STATUSES = ("failed", "skipped")
_CLAIMED_STATUSES = ("claiming", "executing")


@dataclass
class _PacingMetrics:
    counts: dict[str, int] = field(default_factory=lambda: {
        "future": 0,
        "due": 0,
        "late": 0,
        "confirmed": 0,
        "remote_unknown": 0,
        "missed": 0,
        "claimed": 0,
    })
    due_points: list[datetime] = field(default_factory=list)
    effective_points: list[datetime] = field(default_factory=list)
    per_account_executed: dict[int, list[datetime]] = field(default_factory=dict)
    effective_delays: list[float] = field(default_factory=list)
    future_to_now_rewrite_count: int = 0
    release_violation_count: int = 0


def _wall(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(BEIJING_TZ).replace(tzinfo=None)


def task_pacing_summary(session: Session, task) -> dict:
    if getattr(task, "fulfillment_contract_version", "legacy_v1") != "fact_first_v3":
        return {}
    rows = _pacing_rows(session, task)
    sampled = len(rows) > PACING_SUMMARY_SAMPLE_LIMIT
    rows = rows[:PACING_SUMMARY_SAMPLE_LIMIT]
    if not rows:
        return {}
    from app.services._common import _now

    metrics = _collect_metrics(rows, now_at=_now())
    return _summary_payload(
        task,
        metrics,
        slot_count=len(rows),
        sampled=sampled,
    )


def _pacing_rows(session: Session, task) -> list:
    confirmed_at = (
        select(func.max(FulfillmentRemoteFact.observed_at))
        .where(
            FulfillmentRemoteFact.action_id == Action.id,
            FulfillmentRemoteFact.tenant_id == Action.tenant_id,
            FulfillmentRemoteFact.fact_kind.in_((
                "remote_message_observed",
                "view_observed",
                "reaction_observed",
            )),
        )
        .correlate(Action)
        .scalar_subquery()
    )
    return list(session.execute(
        select(
            Action.id,
            Action.account_id,
            Action.status,
            Action.scheduled_at,
            Action.executed_at,
            Action.pacing_due_at,
            Action.release_not_before_at,
            Action.effective_claim_at,
            confirmed_at.label("remote_confirmed_at"),
        )
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.pacing_due_at.is_not(None),
        )
        .order_by(Action.pacing_due_at.asc())
        .limit(PACING_SUMMARY_SAMPLE_LIMIT + 1)
    ))


def _collect_metrics(rows: list, *, now_at: datetime) -> _PacingMetrics:
    metrics = _PacingMetrics()
    for row in rows:
        due = _wall(row.pacing_due_at)
        scheduled = _wall(row.scheduled_at)
        release = _wall(row.release_not_before_at)
        effective = _wall(row.effective_claim_at) or scheduled
        if due is None:
            continue
        metrics.due_points.append(due)
        if effective is not None:
            metrics.effective_points.append(effective)
            metrics.effective_delays.append(max(0.0, (effective - due).total_seconds()))
        metrics.future_to_now_rewrite_count += int(bool(scheduled and scheduled < due))
        metrics.release_violation_count += int(bool(scheduled and release and scheduled < release))
        _record_status(metrics, row, due=due, now_at=now_at)
    return metrics


def _record_status(
    metrics: _PacingMetrics,
    row,
    *,
    due: datetime,
    now_at: datetime,
) -> None:
    status = str(row.status or "")
    if status == "unknown_after_send":
        metrics.counts["remote_unknown"] += 1
        return
    if status in _OPEN_GUARD_STATUSES:
        grace = timedelta(seconds=DUE_TO_LATE_GRACE_SECONDS)
        key = "future" if due > now_at else "due" if due + grace >= now_at else "late"
        metrics.counts[key] += 1
        metrics.counts["claimed"] += int(status in _CLAIMED_STATUSES)
        return
    if status == "success":
        confirmed_at = _wall(row.remote_confirmed_at)
        if confirmed_at is None:
            metrics.counts["remote_unknown"] += 1
            return
        metrics.counts["confirmed"] += 1
        executed = confirmed_at
        if executed is not None and row.account_id:
            metrics.per_account_executed.setdefault(int(row.account_id), []).append(executed)
        return
    metrics.counts["missed"] += int(status in _TERMINAL_MISSED_STATUSES)


def _summary_payload(
    task,
    metrics: _PacingMetrics,
    *,
    slot_count: int,
    sampled: bool,
) -> dict:
    gaps = _account_gaps(metrics.per_account_executed)
    effective_delays = sorted(metrics.effective_delays)
    raw_stats = getattr(task, "stats", {})
    stats = raw_stats if isinstance(raw_stats, dict) else {}
    return {
        "pacing_contract_version": PACING_CONTRACT_VERSION,
        "slot_count": slot_count,
        **metrics.counts,
        **_distribution_payload("due", metrics.due_points, slot_count),
        **_distribution_payload("effective", metrics.effective_points, slot_count),
        "account_min_executed_gap_seconds": gaps[0] if gaps else None,
        "account_executed_gap_p50_seconds": _percentile(gaps, 0.5),
        "account_executed_gap_p95_seconds": _percentile(gaps, 0.95),
        "effective_delay_p50_seconds": _percentile(effective_delays, 0.5),
        "effective_delay_p95_seconds": _percentile(effective_delays, 0.95),
        "future_to_now_rewrite_count": metrics.future_to_now_rewrite_count,
        "release_not_before_violation_count": metrics.release_violation_count,
        "pacing_schedule_shortfall": stats.get("pacing_schedule_shortfall"),
        "sampled": sampled,
    }


def _distribution_payload(prefix: str, points: list[datetime], slot_count: int) -> dict:
    if not points:
        return {
            f"earliest_{prefix}_at": None,
            f"latest_{prefix}_at": None,
            f"same_{prefix}_second_count": 0,
            f"{prefix}_at_unique_ratio": 0.0,
        }
    second_buckets: dict[datetime, int] = {}
    for point in points:
        second = point.replace(microsecond=0)
        second_buckets[second] = second_buckets.get(second, 0) + 1
    payload = {
        f"earliest_{prefix}_at": min(points).isoformat(),
        f"latest_{prefix}_at": max(points).isoformat(),
        f"same_{prefix}_second_count": sum(
            count - 1 for count in second_buckets.values() if count > 1
        ),
        f"{prefix}_at_unique_ratio": round(len(second_buckets) / slot_count, 4),
    }
    if prefix == "due":
        payload["same_second_count"] = payload["same_due_second_count"]
        payload["five_minute_peak"] = _peak_payload(points, slot_count)
    return payload


def _peak_payload(points: list[datetime], total: int) -> dict:
    span_seconds = max(1, int((max(points) - min(points)).total_seconds()))
    upper_bound = min(
        total,
        -(-total * _FIVE_MINUTE_WINDOW_SECONDS // span_seconds) + 1,
    )
    return {
        "count": _five_minute_peak(points),
        "upper_bound": upper_bound,
        "window_seconds": _FIVE_MINUTE_WINDOW_SECONDS,
    }


def _five_minute_peak(points: list[datetime]) -> int:
    ordered = sorted(points)
    window = timedelta(seconds=_FIVE_MINUTE_WINDOW_SECONDS)
    peak = 0
    start = 0
    for end, point in enumerate(ordered):
        while point - ordered[start] >= window:
            start += 1
        peak = max(peak, end - start + 1)
    return peak


def _account_gaps(per_account: dict[int, list[datetime]]) -> list[float]:
    gaps: list[float] = []
    for executed_list in per_account.values():
        ordered = sorted(executed_list)
        gaps.extend(
            delta
            for left, right in zip(ordered, ordered[1:])
            if (delta := (right - left).total_seconds()) > 0
        )
    return sorted(gaps)


def _percentile(sorted_values: list[float], ratio: float) -> float | None:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, round((len(sorted_values) - 1) * ratio)))
    return sorted_values[index]


__all__ = ["PACING_SUMMARY_SAMPLE_LIMIT", "task_pacing_summary"]
