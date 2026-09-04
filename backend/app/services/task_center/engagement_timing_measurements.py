from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from math import ceil

from sqlalchemy import select

from app.models import Action, ExecutionAttempt, ExecutionTimingSample, Tenant
from app.timezone import as_beijing

from .engagement_timing_path import TimingExecutionPath, path_stages


TIMING_POLICY_REVISION = "execution_timing_policy_v1"
PERCENTILE_NUMERATOR = 95
PERCENTILE_DENOMINATOR = 100
SAFETY_MARGIN_FLOOR_MS = 5000
SAFETY_MARGIN_RATIO = 0.20
MILLISECONDS_PER_SECOND = 1000


@dataclass(frozen=True)
class TimingSampleInput:
    tenant_id: int
    adapter: str
    lane: str
    evidence_kind: str
    evidence_reference: str
    evidence_hash: str
    boundaries: dict[str, datetime]
    execution_path: TimingExecutionPath


def record_execution_timing_sample(session, spec: TimingSampleInput) -> ExecutionTimingSample:
    fields = _sample_fields(session, spec)
    lock_timing_tenant(session, spec.tenant_id)
    prior = session.scalar(select(ExecutionTimingSample).where(
        ExecutionTimingSample.tenant_id == spec.tenant_id,
        ExecutionTimingSample.evidence_kind == spec.evidence_kind,
        ExecutionTimingSample.evidence_reference == spec.evidence_reference,
    ))
    if prior is not None:
        if prior.sample_hash != fields["sample_hash"]:
            raise ValueError("execution_timing_evidence_replay_conflict")
        return prior
    sample = ExecutionTimingSample(**fields)
    session.add(sample)
    session.flush()
    return sample


def _sample_fields(session, spec: TimingSampleInput) -> dict:
    execution_path = spec.execution_path.snapshot(adapter=spec.adapter, lane=spec.lane)
    stages = path_stages(spec.adapter, spec.lane)
    if set(spec.boundaries) != set(stages):
        raise ValueError("execution_timing_sample_path_incomplete")
    times = [as_beijing(spec.boundaries[key]) for key in stages]
    if any(value is None for value in times) or any(left > right for left, right in zip(times, times[1:])):
        raise ValueError("execution_timing_sample_order_invalid")
    _validate_evidence(session, spec, times[-1])
    boundaries = {key: value.isoformat() for key, value in zip(stages, times)}
    fields = {
        "tenant_id": spec.tenant_id, "adapter": spec.adapter, "lane": spec.lane,
        "execution_path": execution_path, "execution_path_hash": timing_hash(execution_path),
        "evidence_kind": spec.evidence_kind, "evidence_reference": spec.evidence_reference,
        "evidence_hash": spec.evidence_hash,
        "execution_attempt_id": spec.evidence_reference if spec.evidence_kind == "remote_attempt" else None,
        "boundary_timestamps": boundaries,
        **_duration_fields(stages, times),
        "started_at": times[0], "finished_at": times[-1],
    }
    return {**fields, "sample_hash": timing_hash(fields)}


def _validate_evidence(session, spec: TimingSampleInput, endpoint: datetime) -> None:
    _validate_reference(spec)
    if spec.evidence_kind == "shadow_run":
        return
    if spec.evidence_kind != "remote_attempt" or spec.lane == "classification":
        raise ValueError("execution_timing_evidence_kind_invalid")
    attempt = _matching_attempt(session, spec)
    if attempt.gateway_call_started_at is None or as_beijing(attempt.gateway_call_started_at) != endpoint:
        raise ValueError("execution_timing_attempt_endpoint_mismatch")


def _validate_reference(spec: TimingSampleInput) -> None:
    if not spec.evidence_reference.strip() or len(spec.evidence_hash) != 64 or any(char not in "0123456789abcdef" for char in spec.evidence_hash):
        raise ValueError("execution_timing_evidence_missing")


def _matching_attempt(session, spec: TimingSampleInput) -> ExecutionAttempt:
    attempt = session.get(ExecutionAttempt, spec.evidence_reference)
    action = session.get(Action, attempt.action_id) if attempt else None
    if attempt is None or action is None or action.tenant_id != spec.tenant_id:
        raise ValueError("execution_timing_attempt_missing")
    if action.task_type != spec.adapter or attempt.tenant_id != spec.tenant_id:
        raise ValueError("execution_timing_attempt_scope_mismatch")
    return attempt


def lock_timing_tenant(session, tenant_id: int) -> None:
    session.flush()
    session.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update()).scalar_one()


def nearest_rank_p95(values: list[int]) -> int:
    if not values or any(value < 0 for value in values):
        raise ValueError("execution_timing_sample_values_invalid")
    rank = ceil(len(values) * PERCENTILE_NUMERATOR / PERCENTILE_DENOMINATOR)
    return sorted(values)[rank - 1]


def safety_margin_ms(remaining_ms: int) -> int:
    if remaining_ms < 0:
        raise ValueError("execution_timing_sample_values_invalid")
    # Policy ceil is in whole seconds, not fractional milliseconds.
    seconds = ceil(remaining_ms / MILLISECONDS_PER_SECOND * SAFETY_MARGIN_RATIO)
    return max(SAFETY_MARGIN_FLOOR_MS, seconds * MILLISECONDS_PER_SECOND)


def _elapsed_ms(start: datetime, end: datetime) -> int:
    return ceil((end - start).total_seconds() * MILLISECONDS_PER_SECOND)


def _joint_paths(stages: tuple[str, ...], times: list[datetime]) -> dict:
    return {
        stage: {end: _elapsed_ms(times[index], times[offset]) for offset, end in enumerate(stages) if offset > index}
        for index, stage in enumerate(stages[:-1])
    }


def _duration_fields(stages: tuple[str, ...], times: list[datetime]) -> dict:
    joint = _joint_paths(stages, times)
    return {
        "stage_durations_ms": {key: joint[key][stages[index + 1]] for index, key in enumerate(stages[:-1])},
        "remaining_path_ms": {key: ends[stages[-1]] for key, ends in joint.items()},
        "joint_path_ms": joint,
    }


def timing_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
