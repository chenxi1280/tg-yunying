"""Preview abandoned source reservations; apply only an unchanged explicit snapshot."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import text

from app.database import SessionLocal
from app.services.task_center.stale_source_admissions import (
    RecoveryScope, apply_stale_admissions, preview_stale_admissions,
)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--state-id", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--preview", type=Path, help="Required unchanged preview JSON for apply")
    parser.add_argument("--actor")
    parser.add_argument("--audit-reference")
    parser.add_argument("--output", type=Path, help="Write preview or apply receipt as JSON")
    args = parser.parse_args()
    if args.apply and not (args.preview and args.actor and args.audit_reference):
        parser.error("--apply requires --preview, --actor and --audit-reference")
    if args.preview and not args.apply:
        parser.error("--preview is an apply input; use --output to save a new preview")
    return args


def run(session, args) -> dict:
    scope = RecoveryScope(args.tenant_id, tuple(args.state_id))
    if not args.apply:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        return preview_stale_admissions(session, scope)
    preview = json.loads(args.preview.read_text())
    if preview.get("tenant_id") != scope.tenant_id or preview.get("state_ids") != sorted(scope.state_ids):
        raise ValueError("CLI scope differs from preview")
    return apply_stale_admissions(
        session, preview, actor=args.actor, audit_reference=args.audit_reference,
    )


def main():
    args = parse_args()
    with SessionLocal() as session, session.begin():
        result = run(session, args)
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
