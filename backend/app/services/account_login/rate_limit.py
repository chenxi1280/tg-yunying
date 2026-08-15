from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import TgAccountLoginRateBucket
from app.services._common import _now
from app.timezone import as_beijing_aware


RATE_LEASE_SECONDS = 30


@dataclass(frozen=True)
class RateLease:
    scope_type: str
    scope_id: str
    token: str


@dataclass(frozen=True)
class RateAcquireResult:
    lease: RateLease | None
    retry_at: datetime | None


def acquire_rate_lease(
    session: Session,
    *,
    scope_type: str,
    scope_id: str,
    max_concurrency: int,
    min_interval_seconds: float,
) -> RateAcquireResult:
    if max_concurrency < 1 or min_interval_seconds < 0:
        raise ValueError("invalid account login rate bucket configuration")
    now = _now()
    bucket = _locked_rate_bucket(session, scope_type, scope_id, max_concurrency)
    leases = _active_leases(bucket, now)
    retry_at = _retry_at(bucket, leases, now, max_concurrency)
    if retry_at:
        _persist_leases(bucket, leases)
        session.commit()
        return RateAcquireResult(None, retry_at)
    token = uuid4().hex
    expires_at = now + timedelta(seconds=RATE_LEASE_SECONDS)
    leases[token] = expires_at.isoformat()
    bucket.max_concurrency = max_concurrency
    bucket.next_available_at = now + timedelta(seconds=min_interval_seconds)
    bucket.state_version += 1
    _persist_leases(bucket, leases)
    session.commit()
    return RateAcquireResult(RateLease(scope_type, scope_id, token), None)


def _locked_rate_bucket(
    session: Session,
    scope_type: str,
    scope_id: str,
    max_concurrency: int,
) -> TgAccountLoginRateBucket:
    query = select(TgAccountLoginRateBucket).where(
        TgAccountLoginRateBucket.scope_type == scope_type,
        TgAccountLoginRateBucket.scope_id == scope_id,
    ).with_for_update()
    bucket = session.scalar(query)
    if bucket:
        return bucket
    try:
        with session.begin_nested():
            bucket = TgAccountLoginRateBucket(
                scope_type=scope_type,
                scope_id=scope_id,
                max_concurrency=max_concurrency,
            )
            session.add(bucket)
            session.flush()
        return bucket
    except IntegrityError:
        existing = session.scalar(query)
        if not existing:
            raise
        return existing


def release_rate_lease(session: Session, lease: RateLease) -> None:
    bucket = session.scalar(select(TgAccountLoginRateBucket).where(
        TgAccountLoginRateBucket.scope_type == lease.scope_type,
        TgAccountLoginRateBucket.scope_id == lease.scope_id,
    ).with_for_update())
    if not bucket:
        return
    leases = _active_leases(bucket, _now())
    leases.pop(lease.token, None)
    bucket.state_version += 1
    _persist_leases(bucket, leases)
    session.commit()


def _active_leases(bucket: TgAccountLoginRateBucket, now) -> dict[str, str]:
    try:
        raw = json.loads(bucket.lease_tokens_json or "[]")
    except json.JSONDecodeError:
        raw = {}
    values = raw if isinstance(raw, dict) else {}
    return {token: value for token, value in values.items() if _batch_datetime_from_iso(value) > now}


def _batch_datetime(value: datetime) -> datetime:
    return as_beijing_aware(value).replace(tzinfo=None)


def _batch_datetime_from_iso(value: str) -> datetime:
    return _batch_datetime(datetime.fromisoformat(value))


def _retry_at(bucket: TgAccountLoginRateBucket, leases: dict[str, str], now, max_concurrency: int):
    candidates = []
    if bucket.next_available_at and _batch_datetime(bucket.next_available_at) > now:
        candidates.append(_batch_datetime(bucket.next_available_at))
    if len(leases) >= max_concurrency:
        candidates.append(min(_batch_datetime_from_iso(value) for value in leases.values()))
    return max(candidates) if candidates else None


def _persist_leases(bucket: TgAccountLoginRateBucket, leases: dict[str, str]) -> None:
    bucket.lease_tokens_json = json.dumps(leases, separators=(",", ":"), sort_keys=True)
    bucket.active_leases = len(leases)
    bucket.lease_expires_at = max((_batch_datetime_from_iso(value) for value in leases.values()), default=None)


__all__ = ["RateAcquireResult", "RateLease", "acquire_rate_lease", "release_rate_lease"]
