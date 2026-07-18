# CPU Backpressure Root Cause Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the repeated planner backlog scans and high-frequency runtime-stat scans that are driving PostgreSQL and worker CPU on the Silicon Valley production host.

**Architecture:** Keep the metrics worker loop at its current short interval for liveness, but persist expensive runtime metrics and task/account summaries only every five minutes. Compute global planner backlog once at the start of a planner drain, pass that count through task batches, and conservatively add newly retried and planned open actions before evaluating later tasks. Add concurrent PostgreSQL partial indexes matching the actual open-action predicates, plus a heartbeat timestamp index.

**Tech Stack:** Python 3, SQLAlchemy, PostgreSQL, SQLite test database, Alembic, pytest.

---

### Task 1: Lock the planner and metrics behavior with failing tests

**Files:**
- Modify: `backend/tests/test_task_center_role_drains.py`
- Create: `backend/tests/test_cpu_backpressure_index_migration.py`

- [ ] **Step 1: Write the planner caching regression test**

```python
@pytest.mark.no_postgres
def test_planner_computes_global_backlog_once_per_drain(monkeypatch) -> None:
    SessionFactory = _session_factory()
    global_calls = 0
    original = planner_backlog._active_backlog_metrics

    def count_global(session, filters, now_value):
        nonlocal global_calls
        if len(filters) == 1:
            global_calls += 1
        return original(session, filters, now_value)

    monkeypatch.setattr(planner_backlog, "_active_backlog_metrics", count_global)
    # Seed two due tasks, then execute one planner drain.
    assert service.drain_task_planner(SessionFactory, 2) >= 0
    assert global_calls == 1
```

- [ ] **Step 2: Run the planner test and verify it fails because the old code scans global backlog per task**

Run: `perl -e 'alarm 60; exec @ARGV' /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/pytest -q -m no_postgres backend/tests/test_task_center_role_drains.py::test_planner_computes_global_backlog_once_per_drain`

Expected: FAIL with `global_calls == 2` for two due tasks.

- [ ] **Step 3: Write the five-minute metrics cadence regression test**

```python
@pytest.mark.no_postgres
def test_metrics_drain_keeps_heartbeat_but_defers_expensive_capture(monkeypatch) -> None:
    clock = {"now": datetime(2026, 7, 18, 12, 0, tzinfo=UTC)}
    monkeypatch.setattr(metrics_runtime, "_now", lambda: clock["now"])
    monkeypatch.setattr(heartbeat, "_now", lambda: clock["now"])

    first = metrics_runtime.drain_task_metrics(SessionFactory, 5)
    clock["now"] += timedelta(seconds=30)
    second = metrics_runtime.drain_task_metrics(SessionFactory, 5)
    clock["now"] += timedelta(minutes=5)
    third = metrics_runtime.drain_task_metrics(SessionFactory, 5)

    assert first > 0
    assert second == 0
    assert third > 0
    assert _metrics_heartbeat_seen_at(SessionFactory) == clock["now"]
```

- [ ] **Step 4: Run the metrics test and verify it fails because the old drain records again after 30 seconds**

Run: `perl -e 'alarm 60; exec @ARGV' /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/pytest -q -m no_postgres backend/tests/test_task_center_role_drains.py::test_metrics_drain_keeps_heartbeat_but_defers_expensive_capture`

Expected: FAIL because the second drain records metrics and summaries again.

- [ ] **Step 5: Write the migration contract test**

```python
def test_cpu_backpressure_index_migration_declares_hot_path_indexes() -> None:
    migration = MIGRATIONS / "0104_cpu_backpressure_indexes.py"
    assert migration.exists()
    source = migration.read_text()
    for name in (
        "ix_actions_planner_open_normal_global",
        "ix_actions_planner_open_normal_task",
        "ix_actions_planner_open_hard_hourly_task",
        "ix_worker_heartbeats_last_seen_at",
        "ix_runtime_metric_snapshots_metric_dimension_captured",
    ):
        assert name in source
```

- [ ] **Step 6: Run the migration contract test and verify it fails because the migration does not exist**

Run: `perl -e 'alarm 60; exec @ARGV' /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/pytest -q -m no_postgres backend/tests/test_cpu_backpressure_index_migration.py`

Expected: FAIL at `migration.exists()`.

### Task 2: Deduplicate the planner global backlog calculation

**Files:**
- Modify: `backend/app/services/task_center/planner_backlog.py:20-62`
- Modify: `backend/app/services/task_center/service.py:117,1611-1694,1818-1836`
- Test: `backend/tests/test_task_center_role_drains.py`

- [ ] **Step 1: Add an explicit global-count helper and an optional cached value**

```python
def planner_global_pending(session: Session, now_value: datetime | None = None) -> int:
    count, _ = _active_backlog_metrics(
        session,
        [Action.status.in_(PLANNER_BACKLOG_OPEN_STATUSES)],
        now_value or _now(),
    )
    return count

def planner_backlog_snapshot(
    session: Session,
    task: Task,
    *,
    global_pending: int | None = None,
) -> dict[str, int | bool]:
    now_value = _now()
    active_global = global_pending if global_pending is not None else planner_global_pending(session, now_value)
    # Keep the existing task-local count and oldest-age calculation unchanged.
```

- [ ] **Step 2: Thread the immutable integer through the planner drain**

```python
global_pending = planner_global_pending(session) if task_ids else 0
for task_id in task_ids:
    task_processed, future_open, global_pending = _plan_due_task(
        session_factory,
        task_id,
        process_type,
        limit=limit,
        global_pending=global_pending,
    )
```

Pass the value through `_plan_due_task` and `_plan_due_task_batch`. Add successful retries and the `build_task_plan()` return count to the local value before later tasks are checked. This is deliberately conservative when dispatchers finish actions concurrently, so the global pending ceiling cannot be exceeded because of the cache.

- [ ] **Step 3: Avoid a backlog scan for hard-hourly planning that must bypass it**

```python
def _planning_backlog_blocked(session, task, *, global_pending: int | None = None) -> bool:
    now_value = _now()
    if hard_hourly_requires_planning(session, task, now_value):
        task.stats = clear_planner_backlog_stats(dict(task.stats or {}))
        return False
    snapshot = planner_backlog_snapshot(session, task, global_pending=global_pending)
```

- [ ] **Step 4: Run the planner test and the existing backlog regression tests**

Run: `perl -e 'alarm 60; exec @ARGV' /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/pytest -q -m no_postgres backend/tests/test_task_center_role_drains.py backend/tests/test_task_center_capacity_dispatch.py -k 'planner or backlog'`

Expected: PASS.

### Task 3: Throttle expensive metrics capture without weakening worker liveness

**Files:**
- Modify: `backend/app/services/task_center/metrics_runtime.py:19-39`
- Test: `backend/tests/test_task_center_role_drains.py`

- [ ] **Step 1: Keep the DB heartbeat on every worker loop and gate only expensive work**

```python
METRICS_CAPTURE_INTERVAL = timedelta(minutes=5)
METRICS_CAPTURE_MARKER = "actions.pending.count"

def drain_task_metrics(session_factory, limit: int = 100) -> int:
    now_value = _now()
    with session_factory() as session:
        record_worker_heartbeat(session, process_type="metrics", metadata={"limit": limit})
        if not _runtime_metrics_due(session, now_value):
            session.commit()
            return 0
        record_count = _record_runtime_metrics(session, now_value)
        session.commit()
    return record_count + _refresh_account_summary_batch(session_factory, limit) + _refresh_task_summary_batch(session_factory, limit)
```

- [ ] **Step 2: Use the existing `captured_at` index to decide cadence**

```python
def _runtime_metrics_due(session, now_value: datetime) -> bool:
    latest = session.scalar(
        select(RuntimeMetricSnapshot.captured_at)
        .where(
            RuntimeMetricSnapshot.metric_name == METRICS_CAPTURE_MARKER,
            RuntimeMetricSnapshot.dimension_type == "global",
            RuntimeMetricSnapshot.dimension_id == "all",
        )
        .order_by(RuntimeMetricSnapshot.captured_at.desc())
        .limit(1)
    )
    return latest is None or _elapsed_seconds(latest, now_value) >= METRICS_CAPTURE_INTERVAL.total_seconds()
```

Keep the existing one-minute metric names and values intact; the series becomes five-minute sampled rather than relabeled inaccurately.

- [ ] **Step 3: Run the metrics tests**

Run: `perl -e 'alarm 60; exec @ARGV' /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/pytest -q -m no_postgres backend/tests/test_task_center_role_drains.py -k 'metrics'`

Expected: PASS.

### Task 4: Add safe production indexes for the observed slow paths

**Files:**
- Create: `backend/migrations/versions/0104_cpu_backpressure_indexes.py`
- Test: `backend/tests/test_cpu_backpressure_index_migration.py`

- [ ] **Step 1: Create an idempotent Alembic migration from `0103_group_context_recent_index`**

```python
revision = "0104_cpu_backpressure_indexes"
down_revision = "0103_group_context_recent_index"

INDEX_NAMES = (
    "ix_actions_planner_open_normal_global",
    "ix_actions_planner_open_normal_task",
    "ix_actions_planner_open_hard_hourly_task",
    "ix_worker_heartbeats_last_seen_at",
    "ix_runtime_metric_snapshots_metric_dimension_captured",
)
```

For PostgreSQL, create each index with `CREATE INDEX CONCURRENTLY` inside Alembic's `autocommit_block`. For SQLite, create equivalent partial indexes with `JSON_EXTRACT(payload, '$.hard_hourly_target') IS 1`. Use the same boolean JSON condition that `planner_backlog.py` emits, so PostgreSQL can prove the partial-index predicate.

- [ ] **Step 2: Create the matching index shapes**

```sql
CREATE INDEX CONCURRENTLY ix_actions_planner_open_normal_global
ON actions (scheduled_at, id)
WHERE status IN ('pending', 'claiming', 'executing')
  AND NOT (action_type = 'send_message'
           AND CAST(payload ->> 'hard_hourly_target' AS BOOLEAN) IS TRUE);

CREATE INDEX CONCURRENTLY ix_actions_planner_open_normal_task
ON actions (task_id, scheduled_at, id)
WHERE status IN ('pending', 'claiming', 'executing')
  AND NOT (action_type = 'send_message'
           AND CAST(payload ->> 'hard_hourly_target' AS BOOLEAN) IS TRUE);

CREATE INDEX CONCURRENTLY ix_actions_planner_open_hard_hourly_task
ON actions (task_id, scheduled_at, id)
WHERE status IN ('pending', 'claiming', 'executing')
  AND action_type = 'send_message'
  AND CAST(payload ->> 'hard_hourly_target' AS BOOLEAN) IS TRUE;

CREATE INDEX CONCURRENTLY ix_worker_heartbeats_last_seen_at
ON worker_heartbeats (last_seen_at);

CREATE INDEX CONCURRENTLY ix_runtime_metric_snapshots_metric_dimension_captured
ON runtime_metric_snapshots (metric_name, dimension_type, dimension_id, captured_at DESC);
```

- [ ] **Step 3: Run the migration contract test and a SQLite Alembic upgrade against an empty database**

Run: `perl -e 'alarm 60; exec @ARGV' /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/pytest -q -m no_postgres backend/tests/test_cpu_backpressure_index_migration.py`

Expected: PASS.

### Task 5: Verify, commit, and integrate through the production release path

**Files:**
- Verify: `backend/app/services/task_center/planner_backlog.py`
- Verify: `backend/app/services/task_center/service.py`
- Verify: `backend/app/services/task_center/metrics_runtime.py`
- Verify: `backend/migrations/versions/0104_cpu_backpressure_indexes.py`

- [ ] **Step 1: Run focused regression tests under the repository's 60-second limit**

Run: `perl -e 'alarm 60; exec @ARGV' /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/pytest -q -m no_postgres backend/tests/test_task_center_role_drains.py backend/tests/test_task_center_capacity_dispatch.py backend/tests/test_operations_center_runtime.py backend/tests/test_cpu_backpressure_index_migration.py`

Expected: PASS.

- [ ] **Step 2: Run static verification**

Run: `perl -e 'alarm 60; exec @ARGV' /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/python -m compileall -q backend/app/services/task_center/planner_backlog.py backend/app/services/task_center/metrics_runtime.py backend/app/services/task_center/service.py backend/migrations/versions/0104_cpu_backpressure_indexes.py`

Run: `git diff --check`

Expected: both exit 0.

- [ ] **Step 3: Commit the verified changes, merge through `master -> release`, and use GitHub Actions for deployment**

Verify the GitHub Actions deployment and then collect fresh production E4 evidence: host load, PostgreSQL/container CPU, `EXPLAIN (ANALYZE, BUFFERS)` for planner queries, and five-minute cadence from `runtime_metric_snapshots`.
