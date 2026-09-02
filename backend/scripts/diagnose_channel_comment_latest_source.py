from __future__ import annotations

import argparse
import json

from app.database import SessionLocal
from app.integrations.telegram import gateway
from app.services._common import _now
from app.services.developer_apps import credentials_for_task_account
from app.services.task_center.channel_comment_source_diagnostic import (
    LatestSourceDiagnosticDependencies,
    LatestSourceDiagnosticRequest,
    diagnose_latest_channel_source,
)


def main() -> None:
    args = _parse_args()
    with SessionLocal() as session:
        result = diagnose_latest_channel_source(
            session,
            LatestSourceDiagnosticRequest(
                tenant_id=args.tenant_id, task_id=args.task_id,
            ),
            LatestSourceDiagnosticDependencies(
                fetch_messages=gateway.fetch_channel_messages,
                credentials_for_account=lambda account, task_type: credentials_for_task_account(
                    session, account, task_type,
                ),
                observed_at=_now(),
            ),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only canonical-listener Telegram/local latest source comparison",
    )
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--task-id", required=True)
    return parser.parse_args()


if __name__ == "__main__":
    main()
