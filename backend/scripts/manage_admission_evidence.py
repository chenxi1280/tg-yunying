"""Preview/apply/readback shared evidence without deleting business records."""
import argparse
import json
import os
from pathlib import Path

from sqlalchemy import text

from app.database import SessionLocal
from app.services.task_center.admission_evidence_maintenance import (
    apply_admission_evidence, preview_admission_evidence, readback_admission_evidence,
)
from app.services.task_center.runtime_storage_maintenance import MaintenanceContext


DEFAULT_BATCH_SIZE = 100


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preview", "apply", "readback"))
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--expected-release-sha", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--approval-ref", required=True)
    args = parser.parse_args()
    context = MaintenanceContext(environment=os.environ.get("APP_ENV", ""),
        expected_release_sha=args.expected_release_sha, current_release_sha=os.environ.get("RELEASE_SHA", ""),
        actor=args.actor, approval_ref=args.approval_ref)
    context.validate()
    with SessionLocal.begin() as session:
        session.execute(text("SET LOCAL statement_timeout = '30s'"))
        session.execute(text("SET LOCAL lock_timeout = '2s'"))
        result = execute(session, args, context)
    print(json.dumps(result, sort_keys=True))


def execute(session, args, context):
    if args.mode == "preview":
        session.execute(text("SET TRANSACTION READ ONLY"))
        return preview_admission_evidence(session, batch_size=args.batch_size)
    if args.mode == "readback":
        session.execute(text("SET TRANSACTION READ ONLY"))
        return readback_admission_evidence(session, fingerprint=args.fingerprint, context=context)
    if args.manifest is None:
        raise ValueError("admission_evidence_manifest_required")
    return apply_admission_evidence(session, manifest=json.loads(args.manifest.read_text()), context=context)


if __name__ == "__main__":
    main()
