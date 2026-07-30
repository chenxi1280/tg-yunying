from __future__ import annotations

import argparse
import json
from datetime import datetime

from app.database import SessionLocal
from app.services.task_center.ai_backlog_abandonment import (
    abandon_ai_historical_backlog,
)


def run(
    *,
    cutoff: datetime,
    apply: bool,
    actor: str,
    task_ids: set[str] | None = None,
) -> dict:
    with SessionLocal() as session:
        result = abandon_ai_historical_backlog(
            session,
            cutoff=cutoff,
            apply=apply,
            actor=actor,
            task_ids=task_ids,
        )
        session.commit() if apply else session.rollback()
        return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview or abandon pre-Gateway AI historical backlog.",
    )
    parser.add_argument("--cutoff", required=True, type=datetime.fromisoformat)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--actor", default="ai-backlog-abandonment")
    parser.add_argument("--task-id", action="append", default=[])
    args = parser.parse_args()
    result = run(
        cutoff=args.cutoff,
        apply=args.apply,
        actor=args.actor,
        task_ids=set(args.task_id) if args.task_id else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
