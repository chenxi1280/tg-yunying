"""Preview and explicitly initialize whole-tenant membership evidence; never activate Tasks."""
import argparse
import json
import os
from pathlib import Path
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.services.account_group_revision_bootstrap import (
    apply_group_revision_bootstrap, preview_group_revisions,
)


QUERY_TIMEOUT_SECONDS = 12
LOCK_TIMEOUT_SECONDS = 5


def _arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preview", "apply"), default="preview")
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--preview-file", type=Path)
    parser.add_argument("--expected-deployed-sha")
    parser.add_argument("--actor")
    parser.add_argument("--audit-reference")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _apply(session, args):
    if not all((args.preview_file, args.expected_deployed_sha, args.actor, args.audit_reference)):
        raise ValueError("apply_requires_preview_file_deployed_sha_actor_and_audit_reference")
    preview = json.loads(args.preview_file.read_text())
    deployed = os.getenv("RELEASE_SHA")
    if deployed != args.expected_deployed_sha or preview.get("deployed_sha") != deployed:
        raise ValueError("account_group_bootstrap_release_mismatch")
    if preview["state"]["tenant_id"] != args.tenant_id:
        raise ValueError("account_group_bootstrap_tenant_mismatch")
    receipt = apply_group_revision_bootstrap(session, preview, actor=args.actor,
        audit_reference=args.audit_reference)
    session.commit()
    with SessionLocal() as verification:
        _start_readonly(verification)
        after = preview_group_revisions(verification, args.tenant_id)
    verified = after["state_hash"] == receipt["after_hash"]
    return {**receipt, "mode": "applied" if verified else "applied_readback_changed",
        "readback_verified": verified, "readback_hash": after["state_hash"], "deployed_sha": deployed}


def _start_readonly(session):
    session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    session.execute(text(f"SET LOCAL statement_timeout = '{QUERY_TIMEOUT_SECONDS}s'"))


def main():
    args = _arguments()
    with SessionLocal() as session:
        if args.mode == "preview":
            _start_readonly(session)
        session.execute(text(f"SET LOCAL statement_timeout = '{QUERY_TIMEOUT_SECONDS}s'"))
        session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_SECONDS}s'"))
        observed_at = session.scalar(text("SELECT now()"))
        result = _apply(session, args) if args.mode == "apply" else {
            **preview_group_revisions(session, args.tenant_id), "mode": "preview",
            "deployed_sha": os.getenv("RELEASE_SHA"), "read_only": True}
    report = {**result, "observed_at": observed_at, "task_activation": "not_performed"}
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, default=str)
    if args.output:
        args.output.write_text(serialized + "\n")
    print(serialized)
    if report["mode"] == "applied_readback_changed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
