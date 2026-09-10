"""Audited representation-only conversion of exact historical snapshots."""
import hashlib
import json

from sqlalchemy import inspect, select

from app.models import AuditLog, PlanningAdmissionSnapshot
from .planning_admission_evidence import admission_paths_digest, shared_admission_evidence


AUDIT_ACTION = "normalize_admission_evidence"
LAYOUT_FIELDS = frozenset({"legacy_account_paths", "account_paths_digest"})


def _rows(session, identities, *, lock=False):
    statement = select(PlanningAdmissionSnapshot).where(
        PlanningAdmissionSnapshot.id.in_(identities),
    ).order_by(PlanningAdmissionSnapshot.id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return list(session.scalars(statement))


def _logical_manifest(rows):
    fields = [column.key for column in inspect(PlanningAdmissionSnapshot).column_attrs
              if column.key not in LAYOUT_FIELDS]
    return [{"id": row.id, "paths_hash": admission_paths_digest(row.account_paths),
             "identity_hash": _hash({field: getattr(row, field) for field in fields})} for row in rows]


def _hash(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def preview_admission_evidence(session, *, batch_size: int):
    if batch_size <= 0:
        raise ValueError("admission_evidence_batch_size_invalid")
    ids = list(session.scalars(select(PlanningAdmissionSnapshot.id).where(
        PlanningAdmissionSnapshot.account_paths_digest.is_(None),
    ).order_by(PlanningAdmissionSnapshot.id).limit(batch_size)))
    items = _logical_manifest(_rows(session, ids))
    return {"items": items, "fingerprint": _hash(items), "count": len(items)}


def apply_admission_evidence(session, *, manifest: dict, context):
    context.validate()
    items = manifest["items"]
    if not items or manifest["fingerprint"] != _hash(items) or manifest["count"] != len(items):
        raise ValueError("admission_evidence_manifest_invalid")
    rows = _rows(session, [item["id"] for item in items], lock=True)
    if _logical_manifest(rows) != items or any(row.account_paths_digest is not None for row in rows):
        raise RuntimeError("admission_evidence_snapshot_drift")
    for row in rows:
        evidence = shared_admission_evidence(session, row.tenant_id, row.account_paths)
        if evidence.account_paths != row.account_paths:
            raise RuntimeError("admission_evidence_content_mismatch")
        row.paths_evidence = evidence
        row.account_paths_digest = evidence.digest
        row.legacy_account_paths = []
    session.flush()
    if _logical_manifest(rows) != items:
        raise RuntimeError("admission_evidence_logical_content_changed")
    session.add(AuditLog(actor=context.actor, action=AUDIT_ACTION,
        target_type="planning_admission_snapshot", target_id=manifest["fingerprint"],
        detail=json.dumps({**manifest, "release_sha": context.current_release_sha,
                           "approval_ref": context.approval_ref}, sort_keys=True)))
    session.flush()
    return {"count": len(rows), "fingerprint": manifest["fingerprint"]}


def readback_admission_evidence(session, *, fingerprint: str, context):
    context.validate()
    audit = session.scalar(select(AuditLog).where(
        AuditLog.action == AUDIT_ACTION, AuditLog.target_id == fingerprint,
        AuditLog.actor == context.actor,
    ).order_by(AuditLog.id.desc()).limit(1))
    if audit is None:
        raise RuntimeError("admission_evidence_audit_missing")
    manifest = json.loads(audit.detail)
    if manifest["approval_ref"] != context.approval_ref:
        raise RuntimeError("admission_evidence_audit_reference_mismatch")
    rows = _rows(session, [item["id"] for item in manifest["items"]])
    verified = (_logical_manifest(rows) == manifest["items"] and
                all(row.account_paths_digest is not None and row.legacy_account_paths == [] for row in rows))
    return {"count": len(rows), "audit_id": audit.id, "fingerprint": fingerprint,
            "persisted_verified": verified}
