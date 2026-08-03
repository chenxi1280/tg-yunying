from __future__ import annotations

import json
import os
from datetime import date

from app.database import SessionLocal
from app.services.task_center.ai_reply_ratio_recovery import (
    apply_reply_ratio_recovery,
    build_reply_ratio_recovery_snapshot,
    reply_ratio_recovery_state_hash,
)


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _apply_requested() -> bool:
    value = os.getenv("AI_REPLY_RATIO_RECOVERY_APPLY", "false").lower()
    if value not in {"true", "false"}:
        raise ValueError("AI_REPLY_RATIO_RECOVERY_APPLY must be true or false")
    return value == "true"


def main() -> None:
    apply = _apply_requested()
    target_date = date.fromisoformat(_required("AI_REPLY_RATIO_RECOVERY_DATE"))
    task_ids = tuple(item.strip() for item in _required("AI_REPLY_RATIO_RECOVERY_TASK_IDS").split(",") if item.strip())
    limit = int(os.getenv("AI_REPLY_RATIO_RECOVERY_LIMIT", "5"))
    actor = _required("AI_REPLY_RATIO_RECOVERY_APPROVAL_REF")
    with SessionLocal() as session:
        snapshot = build_reply_ratio_recovery_snapshot(
            session,
            task_ids=task_ids,
            target_date=target_date,
            per_task_limit=limit,
        )
        state_hash = reply_ratio_recovery_state_hash(snapshot)
        result = {"mode": "preview", "state_hash": state_hash, "snapshot": snapshot}
        if apply:
            expected = _required("AI_REPLY_RATIO_RECOVERY_EXPECTED_STATE_HASH")
            applied = apply_reply_ratio_recovery(
                session,
                snapshot=snapshot,
                expected_state_hash=expected,
                actor=actor,
            )
            result = {**result, "mode": "apply", "applied": applied}
    print("AI_REPLY_RATIO_RECOVERY=" + json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
