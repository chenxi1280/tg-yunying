from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.models import ExecutionTimingProfileRevision, ExecutionTimingSample
from app.timezone import as_beijing

from .engagement_timing_measurements import (
    TIMING_POLICY_REVISION, lock_timing_tenant,
    nearest_rank_p95, safety_margin_ms, timing_hash,
)
from .engagement_timing_path import TimingExecutionPath


class ExecutionTimingProfileUnproven(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"execution_timing_profile_unproven:{reason}")


@dataclass(frozen=True)
class TimingProfileApproval:
    tenant_id: int
    adapter: str
    lane: str
    sample_ids: tuple[str, ...]
    minimum_sample_count: int
    approved_by: str
    approval_reference: str
    effective_at: datetime
    valid_until: datetime
    execution_path: TimingExecutionPath


@dataclass(frozen=True)
class DerivedExecutionTiming:
    profile_id: str
    profile_revision: int
    policy_revision: str
    path_start_stage: str
    path_end_stage: str
    complete_remaining_path_p95_ms: int
    safety_margin_ms: int
    protected_slack_ms: int
    latest_start_at: datetime
    deadline_at: datetime
    derived_at: datetime


def publish_execution_timing_profile(session, approval: TimingProfileApproval):
    _validate_approval(approval)
    lock_timing_tenant(session, approval.tenant_id)
    samples = _approved_samples(session, approval)
    fields = _profile_fields(approval, samples)
    prior = session.scalar(select(ExecutionTimingProfileRevision).where(
        ExecutionTimingProfileRevision.tenant_id == approval.tenant_id,
        ExecutionTimingProfileRevision.adapter == approval.adapter,
        ExecutionTimingProfileRevision.lane == approval.lane,
        ExecutionTimingProfileRevision.input_hash == fields["input_hash"],
    ))
    if prior is not None:
        return prior
    active = _active_profile(session, approval)
    if active is not None:
        if as_beijing(approval.effective_at) < as_beijing(active.effective_at):
            raise ValueError("execution_timing_profile_backdated_successor")
        active.state = "superseded"
        session.flush()
    profile = ExecutionTimingProfileRevision(
        **fields, profile_revision=_next_revision(session, approval),
        supersedes_profile_id=active.id if active else None,
    )
    session.add(profile)
    session.flush()
    return profile


def require_execution_timing_profile(
    session, tenant_id: int, *, adapter: str, lane: str, at: datetime, execution_path: TimingExecutionPath,
):
    path_hash = timing_hash(execution_path.snapshot(adapter=adapter, lane=lane))
    current = as_beijing(at)
    profile = session.scalar(select(ExecutionTimingProfileRevision).where(
        ExecutionTimingProfileRevision.tenant_id == tenant_id,
        ExecutionTimingProfileRevision.adapter == adapter,
        ExecutionTimingProfileRevision.lane == lane,
        ExecutionTimingProfileRevision.execution_path_hash == path_hash,
        ExecutionTimingProfileRevision.state.in_(("active", "superseded")),
        ExecutionTimingProfileRevision.effective_at <= current,
    ).order_by(ExecutionTimingProfileRevision.profile_revision.desc()).limit(1))
    if profile is None:
        raise ExecutionTimingProfileUnproven("approved_current_profile_missing")
    if current >= as_beijing(profile.valid_until):
        raise ExecutionTimingProfileUnproven("profile_expired")
    return profile


def derive_execution_deadline(
    profile: ExecutionTimingProfileRevision, deadline_at: datetime, *, path_start_stage: str, derived_at: datetime,
    path_end_stage: str | None = None,
) -> DerivedExecutionTiming:
    end_stage = path_end_stage or profile.execution_path["stages"][-1]
    paths = profile.joint_path_p95_ms.get(path_start_stage, {})
    if end_stage not in paths:
        raise ExecutionTimingProfileUnproven("path_start_stage_missing")
    if profile.state not in {"active", "superseded"} or not profile.approval_reference:
        raise ExecutionTimingProfileUnproven("profile_not_approved")
    duration = int(paths[end_stage])
    margin = safety_margin_ms(duration)
    slack = duration + margin
    deadline = as_beijing(deadline_at)
    return DerivedExecutionTiming(
        profile_id=profile.id, profile_revision=profile.profile_revision, policy_revision=profile.policy_revision,
        path_start_stage=path_start_stage, path_end_stage=end_stage,
        complete_remaining_path_p95_ms=duration, safety_margin_ms=margin, protected_slack_ms=slack,
        latest_start_at=deadline - timedelta(milliseconds=slack), deadline_at=deadline, derived_at=as_beijing(derived_at),
    )


def _validate_approval(approval: TimingProfileApproval) -> None:
    approval.execution_path.snapshot(adapter=approval.adapter, lane=approval.lane)
    if not approval.approved_by.strip() or not approval.approval_reference.strip():
        raise ExecutionTimingProfileUnproven("approval_missing")
    if approval.minimum_sample_count < 1 or len(approval.sample_ids) < approval.minimum_sample_count:
        raise ExecutionTimingProfileUnproven("sample_count_insufficient")
    if len(set(approval.sample_ids)) != len(approval.sample_ids):
        raise ValueError("execution_timing_duplicate_sample")
    if as_beijing(approval.valid_until) <= as_beijing(approval.effective_at):
        raise ValueError("execution_timing_profile_validity_invalid")


def _approved_samples(session, approval):
    rows = list(session.scalars(select(ExecutionTimingSample).where(
        ExecutionTimingSample.id.in_(approval.sample_ids),
        ExecutionTimingSample.tenant_id == approval.tenant_id,
        ExecutionTimingSample.adapter == approval.adapter,
        ExecutionTimingSample.lane == approval.lane,
        ExecutionTimingSample.execution_path_hash == _approval_path_hash(approval),
    ).order_by(ExecutionTimingSample.id)))
    if len(rows) != len(approval.sample_ids):
        raise ExecutionTimingProfileUnproven("sample_scope_or_identity_mismatch")
    if any(as_beijing(row.finished_at) > as_beijing(approval.effective_at) for row in rows):
        raise ValueError("execution_timing_sample_after_effective_at")
    return rows


def _profile_fields(approval, samples):
    fields = {
        "tenant_id": approval.tenant_id, "adapter": approval.adapter, "lane": approval.lane,
        "policy_revision": TIMING_POLICY_REVISION, "sample_ids": [row.id for row in samples],
        "execution_path": approval.execution_path.snapshot(adapter=approval.adapter, lane=approval.lane),
        "execution_path_hash": _approval_path_hash(approval),
        "sample_manifest_hash": timing_hash([(row.id, row.sample_hash) for row in samples]),
        "sample_count": len(samples), "minimum_sample_count": approval.minimum_sample_count,
        "sample_window_start_at": min(as_beijing(row.started_at) for row in samples),
        "sample_window_end_at": max(as_beijing(row.finished_at) for row in samples),
        **_profile_statistics(samples),
        "confidence": "measured" if all(row.evidence_kind == "remote_attempt" for row in samples) else "shadow_approved",
        "approved_by": approval.approved_by, "approval_reference": approval.approval_reference,
        "effective_at": as_beijing(approval.effective_at), "valid_until": as_beijing(approval.valid_until),
    }
    return {**fields, "input_hash": timing_hash(fields)}


def _active_profile(session, approval):
    return session.scalar(select(ExecutionTimingProfileRevision).where(
        ExecutionTimingProfileRevision.tenant_id == approval.tenant_id,
        ExecutionTimingProfileRevision.adapter == approval.adapter,
        ExecutionTimingProfileRevision.lane == approval.lane,
        ExecutionTimingProfileRevision.execution_path_hash == _approval_path_hash(approval),
        ExecutionTimingProfileRevision.state == "active",
    ))


def _next_revision(session, approval) -> int:
    value = session.scalar(select(func.max(ExecutionTimingProfileRevision.profile_revision)).where(
        ExecutionTimingProfileRevision.tenant_id == approval.tenant_id,
        ExecutionTimingProfileRevision.adapter == approval.adapter,
        ExecutionTimingProfileRevision.lane == approval.lane,
        ExecutionTimingProfileRevision.execution_path_hash == _approval_path_hash(approval),
    ))
    return int(value or 0) + 1


def _approval_path_hash(approval: TimingProfileApproval) -> str:
    return timing_hash(approval.execution_path.snapshot(adapter=approval.adapter, lane=approval.lane))


def _joint_path_percentiles(samples) -> dict:
    return {
        start: {end: nearest_rank_p95([row.joint_path_ms[start][end] for row in samples]) for end in ends}
        for start, ends in samples[0].joint_path_ms.items()
    }


def _profile_statistics(samples) -> dict:
    stages = samples[0].execution_path["stages"]
    joint = _joint_path_percentiles(samples)
    paths = {key: ends[stages[-1]] for key, ends in joint.items()}
    return {
        "stage_p95_ms": {key: joint[key][stages[index + 1]] for index, key in enumerate(stages[:-1])},
        "remaining_path_p95_ms": paths, "joint_path_p95_ms": joint,
        "safety_margin_ms": {key: safety_margin_ms(value) for key, value in paths.items()},
    }
