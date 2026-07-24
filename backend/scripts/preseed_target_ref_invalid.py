from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.models import OperationTarget
from app.services.task_center.target_lifecycle import mark_target_ref_invalid


def preseed_target_ref_invalid(
    *,
    tenant_id: int,
    target_id: int,
    expected_peer_id: str,
    expected_username: str,
    expected_lifecycle_version: int,
    reason: str,
    evidence_ref: str,
    actor: str,
) -> dict[str, int | str]:
    with SessionLocal() as session:
        target = session.get(OperationTarget, target_id)
        if target is None or target.tenant_id != tenant_id:
            raise ValueError("target not found")
        if target.tg_peer_id != expected_peer_id:
            raise ValueError("target peer does not match the approved preseed input")
        if str(target.username or "").lstrip("@") != expected_username.lstrip("@"):
            raise ValueError("target username does not match the approved preseed input")
        result = mark_target_ref_invalid(
            session,
            target=target,
            actor=actor,
            reason=reason,
            evidence_ref=evidence_ref,
            expected_version=expected_lifecycle_version,
        )
        session.commit()
        return {
            "target_id": result.target.id,
            "lifecycle_status": result.target.lifecycle_status,
            "reference_revision": int(result.target.reference_revision or 1),
            "lifecycle_version": int(result.target.lifecycle_version or 1),
            "skipped_actions": result.skipped_actions,
            "skipped_message_tasks": result.skipped_message_tasks,
            "skipped_operation_tasks": result.skipped_operation_tasks,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mark one exact verified target reference invalid before enabling the outbound gate.")
    parser.add_argument("--tenant-id", required=True, type=int)
    parser.add_argument("--target-id", required=True, type=int)
    parser.add_argument("--expected-peer-id", required=True)
    parser.add_argument("--expected-username", required=True)
    parser.add_argument("--expected-lifecycle-version", required=True, type=int)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--evidence-ref", required=True)
    parser.add_argument("--actor", default="continuity-preseed")
    args = parser.parse_args(argv)
    result = preseed_target_ref_invalid(
        tenant_id=args.tenant_id,
        target_id=args.target_id,
        expected_peer_id=args.expected_peer_id,
        expected_username=args.expected_username,
        expected_lifecycle_version=args.expected_lifecycle_version,
        reason=args.reason,
        evidence_ref=args.evidence_ref,
        actor=args.actor,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
