from __future__ import annotations

import json
import os

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Action
from app.services.task_center.group_ai_scope import (
    validate_group_ai_content_scope,
)
from app.services.task_center.payloads import (
    SendMessagePayload,
    validate_action_payload,
)


TASK_IDS_ENV = "AI_SCOPE_TASK_IDS"
SAMPLE_LIMIT = 20


def _task_ids() -> list[str]:
    values = [value.strip() for value in os.getenv(TASK_IDS_ENV, "").split(",")]
    task_ids = list(dict.fromkeys(value for value in values if value))
    if not task_ids:
        raise ValueError(f"{TASK_IDS_ENV} is required")
    return task_ids


def _snapshot(session, action: Action) -> dict:
    payload = validate_action_payload(action.action_type, action.payload or {})
    if not isinstance(payload, SendMessagePayload):
        raise RuntimeError("scope_diagnostic_payload_invalid")
    violation = validate_group_ai_content_scope(
        session,
        action,
        payload=payload,
        account_id=action.account_id,
    )
    return {
        "action_id": action.id,
        "task_id": action.task_id,
        "account_id": action.account_id,
        "created_at": action.created_at.isoformat(),
        "violation_field": violation.field if violation else "",
        "violation_detail": violation.detail if violation else "",
        "chat_id": str(payload.chat_id),
        "group_id": payload.group_id,
        "scope_tenant_id": payload.content_scope_tenant_id,
        "scope_group_id": payload.content_scope_group_id,
        "scope_task_id": payload.content_scope_task_id,
        "context_message_ids": payload.context_message_ids,
        "anchor_message_ids": payload.anchor_message_ids,
        "context_snapshot_message_id": payload.context_snapshot_message_id,
        "reply_to_message_id": payload.reply_to_message_id,
        "ai_message_memory_id": payload.ai_message_memory_id,
    }


def main() -> int:
    with SessionLocal() as session:
        actions = list(session.scalars(
            select(Action)
            .where(
                Action.task_id.in_(_task_ids()),
                Action.action_type == "send_message",
                Action.result["error_code"].as_string()
                == "cross_group_content_scope_mismatch",
            )
            .order_by(Action.created_at.desc())
            .limit(SAMPLE_LIMIT)
        ))
        for action in actions:
            print(
                "AI_SCOPE_MISMATCH="
                + json.dumps(_snapshot(session, action), ensure_ascii=False)
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
