from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, RuntimeCleanupAudit
from app.services.task_center.runtime_retention import (
    _attempt_analysis,
    _candidate_fingerprint,
    _runtime_detail_batch,
    cleanup_runtime_details,
)
from app.services.task_center.runtime_retention_policy import (
    DEFAULT_RUNTIME_ACTION_RETENTION_POLICY,
    RuntimeActionRetentionPolicy,
)


@dataclass(frozen=True)
class MaintenanceContext:
    environment: str
    expected_release_sha: str
    current_release_sha: str
    actor: str
    approval_ref: str

    def validate(self) -> None:
        if self.environment != "production":
            raise ValueError("runtime_storage_environment_mismatch")
        if not self.expected_release_sha or self.expected_release_sha != self.current_release_sha:
            raise ValueError("runtime_storage_release_sha_mismatch")
        if not self.actor.strip() or not self.approval_ref.strip():
            raise ValueError("runtime_storage_audit_identity_required")


def preview_runtime_details(
    session: Session,
    *,
    policy: RuntimeActionRetentionPolicy = DEFAULT_RUNTIME_ACTION_RETENTION_POLICY,
    as_of: datetime,
    batch_size: int = 100,
) -> dict:
    cutoffs = policy.cutoffs(as_of)
    rows = _runtime_detail_batch(session, cutoffs, max(1, int(batch_size)), lock=False)
    reasons, protected_attempts = _attempt_analysis(session, rows)
    return {
        "policy_version": policy.version,
        "retention_days": policy.retention_days(),
        "as_of": as_of.isoformat(),
        "cutoffs": {status: cutoff.isoformat() for status, cutoff in cutoffs.items()},
        "candidate_count": len(rows),
        "candidate_ids": [row.id for row in rows],
        "candidate_fingerprint": _candidate_fingerprint(rows, reasons),
        "status_counts": dict(Counter(str(row.status) for row in rows)),
        "protected_attempts": protected_attempts,
        "apply_allowed": not protected_attempts,
    }


def apply_runtime_details_batch(
    session: Session,
    *,
    context: MaintenanceContext,
    as_of: datetime,
    expected_fingerprint: str,
    expected_count: int,
    policy: RuntimeActionRetentionPolicy = DEFAULT_RUNTIME_ACTION_RETENTION_POLICY,
    batch_size: int = 100,
) -> dict:
    context.validate()
    preview = preview_runtime_details(session, policy=policy, as_of=as_of, batch_size=batch_size)
    _validate_expected_preview(preview, expected_fingerprint, expected_count)
    affected = cleanup_runtime_details(
        session,
        policy=policy,
        batch_size=batch_size,
        as_of=as_of,
        expected_fingerprint=expected_fingerprint,
        audit_context=_audit_context(context),
    )
    return {**preview, "mode": "apply", "affected_rows": affected}


def readback_runtime_details(
    session: Session,
    *,
    context: MaintenanceContext,
    expected_fingerprint: str,
) -> dict:
    context.validate()
    audit = session.scalar(
        select(RuntimeCleanupAudit)
        .where(RuntimeCleanupAudit.summary["candidate_fingerprint"].as_string() == expected_fingerprint)
        .where(RuntimeCleanupAudit.summary["approval_ref"].as_string() == context.approval_ref)
        .order_by(RuntimeCleanupAudit.created_at.desc())
        .limit(1)
    )
    if audit is None:
        raise RuntimeError("runtime_storage_readback_audit_missing")
    candidate_ids = list((audit.summary or {}).get("candidate_ids") or [])
    remaining = _remaining_candidate_count(session, candidate_ids)
    deleted_actions = int((audit.deleted_counts or {}).get("actions") or 0)
    typed_count = int((audit.summary or {}).get("typed_summary_count") or 0)
    return {
        "mode": "readback",
        "audit_id": audit.id,
        "candidate_count": len(candidate_ids),
        "remaining_candidate_count": remaining,
        "deleted_action_count": deleted_actions,
        "typed_summary_count": typed_count,
        "persisted_verified": remaining == 0 and deleted_actions == typed_count == len(candidate_ids),
    }


def _validate_expected_preview(preview: dict, fingerprint: str, count: int) -> None:
    if not preview["apply_allowed"]:
        raise RuntimeError("runtime_storage_protected_attempt_conflict")
    if preview["candidate_count"] == 0:
        raise RuntimeError("runtime_storage_no_candidates")
    if preview["candidate_count"] != count:
        raise RuntimeError("runtime_storage_candidate_count_drift")
    if not fingerprint or preview["candidate_fingerprint"] != fingerprint:
        raise RuntimeError("runtime_storage_candidate_fingerprint_drift")


def _audit_context(context: MaintenanceContext) -> dict:
    return {
        "environment": context.environment,
        "release_sha": context.current_release_sha,
        "actor": context.actor.strip(),
        "approval_ref": context.approval_ref.strip(),
    }


def _remaining_candidate_count(session: Session, candidate_ids: list[str]) -> int:
    if not candidate_ids:
        return 0
    return int(session.scalar(
        select(func.count(Action.id)).where(Action.id.in_(candidate_ids))
    ) or 0)


__all__ = [
    "MaintenanceContext",
    "apply_runtime_details_batch",
    "preview_runtime_details",
    "readback_runtime_details",
]
