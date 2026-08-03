from __future__ import annotations

import hashlib
import json
import os

from sqlalchemy import text

from app.database import engine


INDEX_NAME = "ix_actions_ai_generation_group_occupancy"
CREATE_INDEX = (
    f"CREATE INDEX CONCURRENTLY {INDEX_NAME} "
    "ON actions (tenant_id, CAST(payload ->> 'group_id' AS INTEGER), id) "
    "WHERE task_type = 'group_ai_chat' "
    "AND action_type = 'send_message' "
    "AND ((status = 'executing' "
    "AND CAST(payload ->> 'ai_generation_status' AS VARCHAR) = 'generating') "
    "OR (status IN ('pending', 'claiming', 'executing') "
    "AND CAST(payload ->> 'ai_generation_status' AS VARCHAR) = 'ready' "
    "AND COALESCE(CAST(payload ->> 'message_text' AS VARCHAR), '') <> ''))"
)
INDEX_STATE_QUERY = text("""
    SELECT index_meta.indisvalid AS valid,
           pg_get_indexdef(index_meta.indexrelid) AS definition
    FROM pg_index index_meta
    JOIN pg_class index_class ON index_class.oid = index_meta.indexrelid
    JOIN pg_namespace namespace ON namespace.oid = index_class.relnamespace
    WHERE index_class.relname = :index_name
      AND namespace.nspname = current_schema()
""")
AI_CLAIM_COUNT_QUERY = text("""
    SELECT COUNT(*)
    FROM actions
    WHERE status = 'executing'
      AND (claim_owner LIKE 'ai-generation:%'
           OR lease_owner LIKE 'ai-generation:%')
""")


def _apply_requested() -> bool:
    value = os.getenv("AI_GENERATION_OCCUPANCY_INDEX_APPLY", "false").lower()
    if value not in {"true", "false"}:
        raise ValueError("AI_GENERATION_OCCUPANCY_INDEX_APPLY must be true or false")
    return value == "true"


def _snapshot(connection) -> dict:
    row = connection.execute(
        INDEX_STATE_QUERY,
        {"index_name": INDEX_NAME},
    ).mappings().one_or_none()
    return {
        "index_name": INDEX_NAME,
        "index_exists": row is not None,
        "index_valid": bool(row and row["valid"]),
        "index_definition": str(row["definition"] if row else ""),
        "ai_generation_claim_count": int(
            connection.execute(AI_CLAIM_COUNT_QUERY).scalar_one()
        ),
    }


def _state_hash(snapshot: dict) -> str:
    encoded = json.dumps(snapshot, ensure_ascii=True, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_apply_guard(snapshot: dict, expected_hash: str) -> None:
    if _state_hash(snapshot) != expected_hash:
        raise RuntimeError("AI generation occupancy index state hash changed")
    if snapshot["ai_generation_claim_count"] != 0:
        raise RuntimeError("AI generation claims appeared; refusing index recovery")
    if snapshot["index_exists"] and not snapshot["index_valid"]:
        raise RuntimeError("invalid AI generation group occupancy index already exists")


def main() -> None:
    apply = _apply_requested()
    expected_hash = os.getenv(
        "AI_GENERATION_OCCUPANCY_INDEX_EXPECTED_STATE_HASH", "",
    ).strip()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        before = _snapshot(connection)
        before_hash = _state_hash(before)
        if apply:
            _require_apply_guard(before, expected_hash)
            if not before["index_exists"]:
                connection.execute(text(CREATE_INDEX))
        after = _snapshot(connection)
    if apply and not after["index_valid"]:
        raise RuntimeError("AI generation group occupancy index is not valid after apply")
    print("AI_GENERATION_OCCUPANCY_INDEX_RECOVERY=" + json.dumps({
        "mode": "apply" if apply else "preview",
        "state_hash": before_hash,
        "before": before,
        "after": after,
        "applied": bool(apply and not before["index_exists"]),
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
