from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
import re
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo


POLICY_VERSION = "runtime_action_retention_v2"
ROLLOUT_HOLD_POLICY_VERSION = "runtime_action_retention_rollout_hold_v1"
BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")
PROTECTED_ATTEMPT_STATUSES = frozenset({"pending", "gateway_call_started", "result_unknown"})
_MINIMUM_RETENTION_DAYS = {"skipped": 1, "success": 2, "failed": 7}
_REASON_KEYS = (
    "reason_code",
    "error_code",
    "failure_type",
    "skip_reason",
    "generation_outcome",
)
_INVALID_REASON_CHARS = re.compile(r"[^A-Za-z0-9_.:-]+")
_MAX_REASON_LENGTH = 80


@dataclass(frozen=True)
class RuntimeActionRetentionPolicy:
    skipped_days: int = 1
    success_days: int = 2
    failed_days: int = 7
    version: str = POLICY_VERSION

    def __post_init__(self) -> None:
        values = self.retention_days()
        invalid = [status for status, minimum in _MINIMUM_RETENTION_DAYS.items() if values[status] < minimum]
        if invalid:
            raise ValueError(f"runtime retention below contract minimum: {','.join(invalid)}")

    def retention_days(self) -> dict[str, int]:
        return {
            "skipped": int(self.skipped_days),
            "success": int(self.success_days),
            "failed": int(self.failed_days),
        }

    def cutoffs(self, as_of: datetime) -> dict[str, datetime]:
        local_as_of = _aware_utc(as_of).astimezone(BUSINESS_TIMEZONE)
        local_midnight = datetime.combine(local_as_of.date(), time.min, BUSINESS_TIMEZONE)
        return {
            status: (local_midnight - timedelta(days=days)).astimezone(timezone.utc)
            for status, days in self.retention_days().items()
        }


@dataclass(frozen=True)
class RetentionCandidate:
    id: str
    status: str
    age_at: datetime
    action_type: str
    reason_code: str


DEFAULT_RUNTIME_ACTION_RETENTION_POLICY = RuntimeActionRetentionPolicy()


def configured_runtime_action_retention_policy(
    *,
    enabled: bool,
    skipped_days: int,
    success_days: int,
    failed_days: int,
) -> RuntimeActionRetentionPolicy:
    if enabled:
        return RuntimeActionRetentionPolicy(
            skipped_days=skipped_days,
            success_days=success_days,
            failed_days=failed_days,
        )
    return RuntimeActionRetentionPolicy(
        skipped_days=5,
        success_days=5,
        failed_days=7,
        version=ROLLOUT_HOLD_POLICY_VERSION,
    )


def terminal_reason_code(result: Mapping | None, attempt_failure: str = "") -> str:
    values = result if isinstance(result, Mapping) else {}
    for key in _REASON_KEYS:
        normalized = _normalize_reason(values.get(key))
        if normalized:
            return normalized
    return _normalize_reason(attempt_failure) or "unclassified"


def candidate_fingerprint(candidates: Iterable[RetentionCandidate]) -> str:
    digest = sha256()
    for candidate in sorted(candidates, key=lambda item: item.id):
        parts = (
            candidate.id,
            candidate.status,
            _aware_utc(candidate.age_at).isoformat(),
            candidate.action_type,
            candidate.reason_code,
        )
        digest.update("\x1f".join(parts).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _normalize_reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    normalized = _INVALID_REASON_CHARS.sub("_", value.strip()).strip("_")
    return normalized[:_MAX_REASON_LENGTH]


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=BUSINESS_TIMEZONE).astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "DEFAULT_RUNTIME_ACTION_RETENTION_POLICY",
    "POLICY_VERSION",
    "PROTECTED_ATTEMPT_STATUSES",
    "RetentionCandidate",
    "RuntimeActionRetentionPolicy",
    "candidate_fingerprint",
    "configured_runtime_action_retention_policy",
    "terminal_reason_code",
]
