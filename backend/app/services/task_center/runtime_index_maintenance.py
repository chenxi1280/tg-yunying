from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.services.task_center.runtime_storage_maintenance import MaintenanceContext


OLD_AI_MEMORY_INDEX = "ix_ai_group_message_memory_tenant_status_updated"
NEW_AI_MEMORY_INDEX = "ix_ai_group_message_memory_account_updated"
ACTION_REINDEX_ALLOWLIST = frozenset({
    "ix_actions_hard_hourly_history_scheduled",
    "ix_actions_runtime_detail_retention",
    "ix_actions_task_schedule_page",
    "ix_actions_task_type_schedule_page",
})
CREATE_AI_MEMORY_INDEX = (
    f"CREATE INDEX CONCURRENTLY {NEW_AI_MEMORY_INDEX} "
    "ON ai_group_message_memory (tenant_id, account_id, updated_at DESC) "
    "INCLUDE (planned_at, status, id)"
)
VACUUM_ANALYZE_ACTIONS = "VACUUM (ANALYZE) actions"
CAPACITY_EVIDENCE_MAX_AGE = timedelta(minutes=30)
CAPACITY_SAFETY_MULTIPLIER = 3


@dataclass(frozen=True)
class IndexState:
    name: str
    exists: bool
    valid: bool
    ready: bool
    size_bytes: int


def preview_runtime_indexes(engine: Engine) -> dict:
    _require_postgres(engine)
    with engine.connect() as connection:
        states = [_index_state(connection, name) for name in _managed_indexes()]
        plan = _incremental_memory_plan(connection) if _valid(states, NEW_AI_MEMORY_INDEX) else []
    payload = {
        "indexes": [asdict(state) for state in states],
        "new_index_plan": plan,
        "new_index_plan_uses_candidate": NEW_AI_MEMORY_INDEX in json.dumps(plan),
    }
    return {**payload, "state_fingerprint": _state_fingerprint(states)}


def create_ai_memory_index(
    engine: Engine,
    *,
    context: MaintenanceContext,
    expected_state_fingerprint: str,
    observed_free_bytes: int,
    capacity_observed_at: datetime,
) -> dict:
    context.validate()
    preview = preview_runtime_indexes(engine)
    _validate_state_fingerprint(preview, expected_state_fingerprint)
    old_size = _state_size(preview, OLD_AI_MEMORY_INDEX)
    _validate_capacity(observed_free_bytes, capacity_observed_at, old_size)
    if _state(preview, NEW_AI_MEMORY_INDEX)["exists"]:
        raise RuntimeError("runtime_index_candidate_already_exists")
    _execute_concurrent_ddl(engine, CREATE_AI_MEMORY_INDEX)
    return preview_runtime_indexes(engine)


def drop_old_ai_memory_index(
    engine: Engine,
    *,
    context: MaintenanceContext,
    expected_state_fingerprint: str,
) -> dict:
    context.validate()
    preview = preview_runtime_indexes(engine)
    _validate_state_fingerprint(preview, expected_state_fingerprint)
    if not _state(preview, NEW_AI_MEMORY_INDEX)["valid"]:
        raise RuntimeError("runtime_index_candidate_not_valid")
    if not preview["new_index_plan_uses_candidate"]:
        raise RuntimeError("runtime_index_candidate_plan_unproven")
    _execute_concurrent_ddl(engine, f"DROP INDEX CONCURRENTLY {OLD_AI_MEMORY_INDEX}")
    return preview_runtime_indexes(engine)


def reindex_action_index(
    engine: Engine,
    *,
    context: MaintenanceContext,
    index_name: str,
    expected_state_fingerprint: str,
    observed_free_bytes: int,
    capacity_observed_at: datetime,
) -> dict:
    context.validate()
    if index_name not in ACTION_REINDEX_ALLOWLIST:
        raise ValueError("runtime_index_not_allowlisted")
    preview = preview_runtime_indexes(engine)
    _validate_state_fingerprint(preview, expected_state_fingerprint)
    _validate_capacity(observed_free_bytes, capacity_observed_at, _state_size(preview, index_name))
    _execute_concurrent_ddl(engine, f"REINDEX INDEX CONCURRENTLY {index_name}")
    return preview_runtime_indexes(engine)


def vacuum_analyze_actions(
    engine: Engine,
    *,
    context: MaintenanceContext,
    expected_state_fingerprint: str,
) -> dict:
    context.validate()
    preview = preview_runtime_indexes(engine)
    _validate_state_fingerprint(preview, expected_state_fingerprint)
    _execute_concurrent_ddl(engine, VACUUM_ANALYZE_ACTIONS)
    return preview_runtime_indexes(engine)


def _index_state(connection, name: str) -> IndexState:
    row = connection.execute(text("""
        SELECT index_meta.indisvalid,
               index_meta.indisready,
               pg_relation_size(index_class.oid) AS size_bytes
        FROM pg_class AS index_class
        JOIN pg_index AS index_meta ON index_meta.indexrelid = index_class.oid
        JOIN pg_namespace AS namespace ON namespace.oid = index_class.relnamespace
        WHERE namespace.nspname = current_schema() AND index_class.relname = :name
    """), {"name": name}).mappings().first()
    if row is None:
        return IndexState(name=name, exists=False, valid=False, ready=False, size_bytes=0)
    return IndexState(
        name=name,
        exists=True,
        valid=bool(row["indisvalid"]),
        ready=bool(row["indisready"]),
        size_bytes=int(row["size_bytes"] or 0),
    )


def _incremental_memory_plan(connection) -> list:
    sample = connection.execute(text("""
        SELECT tenant_id, account_id
        FROM ai_group_message_memory
        WHERE account_id IS NOT NULL
        ORDER BY updated_at DESC
        LIMIT 1
    """)).mappings().first()
    if sample is None:
        return []
    return connection.execute(text("""
        EXPLAIN (FORMAT JSON)
        SELECT id, normalized_text, raw_text, planned_at, status
        FROM ai_group_message_memory
        WHERE tenant_id = :tenant_id
          AND account_id = :account_id
          AND planned_at >= CURRENT_TIMESTAMP - INTERVAL '10 days'
          AND updated_at >= CURRENT_TIMESTAMP - INTERVAL '30 days'
        ORDER BY updated_at DESC
    """), dict(sample)).scalar_one()


def _execute_concurrent_ddl(engine: Engine, statement: str) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("SET lock_timeout = '5s'"))
        connection.execute(text(statement))


def _validate_capacity(observed: int, observed_at: datetime, index_size: int) -> None:
    now_value = datetime.now(timezone.utc)
    evidence_at = observed_at.astimezone(timezone.utc)
    if evidence_at > now_value + timedelta(minutes=5):
        raise ValueError("runtime_index_capacity_evidence_from_future")
    if now_value - evidence_at > CAPACITY_EVIDENCE_MAX_AGE:
        raise ValueError("runtime_index_capacity_evidence_stale")
    required = max(1, index_size) * CAPACITY_SAFETY_MULTIPLIER
    if int(observed) < required:
        raise RuntimeError(f"runtime_index_capacity_insufficient:{observed}:{required}")


def _state_fingerprint(states: list[IndexState]) -> str:
    payload = json.dumps([asdict(state) for state in states], sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _validate_state_fingerprint(preview: dict, expected: str) -> None:
    if not expected or preview["state_fingerprint"] != expected:
        raise RuntimeError("runtime_index_state_fingerprint_drift")


def _state(preview: dict, name: str) -> dict:
    return next(item for item in preview["indexes"] if item["name"] == name)


def _state_size(preview: dict, name: str) -> int:
    state = _state(preview, name)
    if not state["exists"] or not state["valid"]:
        raise RuntimeError(f"runtime_index_source_invalid:{name}")
    return int(state["size_bytes"])


def _valid(states: list[IndexState], name: str) -> bool:
    return any(state.name == name and state.valid for state in states)


def _managed_indexes() -> tuple[str, ...]:
    return (OLD_AI_MEMORY_INDEX, NEW_AI_MEMORY_INDEX, *sorted(ACTION_REINDEX_ALLOWLIST))


def _require_postgres(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        raise RuntimeError("runtime_index_maintenance_requires_postgresql")
