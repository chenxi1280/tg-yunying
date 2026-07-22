# AI Group Capacity Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` inline. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure AI active-group sends cannot exceed a group’s daily limit or cooldown, then restore the four all-account daily-coverage tasks with capacity that matches their current 675-account obligation.

**Architecture:** Keep the existing planner capacity blocker as the source of truth for all-account coverage; it must remain fail-closed when group capacity is insufficient. Add a final, gateway-adjacent reservation in the Task Center dispatcher: under a row lock on `TgGroup`, it counts durable `ExecutionAttempt` slots for the Beijing day, creates the next `before_call` attempt only when both group `daily_limit` and cooldown allow it, commits, and only then calls Telegram. This handles concurrent dispatcher workers without silently increasing group limits.

**Tech Stack:** Python 3, SQLAlchemy, PostgreSQL/SQLite test suite, pytest, GitHub Actions production deployment.

---

### Task 1: Lock the missing runtime gate with failing dispatcher tests

**Files:**
- Create: `backend/tests/test_group_ai_send_limits.py`
- Verify: `backend/.venv/bin/pytest backend/tests/test_group_ai_send_limits.py -q`

- [x] **Step 1: Add a daily-limit regression test**

```python
@pytest.mark.no_postgres
def test_group_ai_send_respects_group_daily_limit(monkeypatch):
    # Seed one successful group Action + ExecutionAttempt today and daily_limit=1.
    # Dispatch a second AI group Action with all ordinary preconditions satisfied.
    # Assert Telegram gateway is not called and the Action is deferred with SLOWMODE.
```

- [x] **Step 2: Run the test before production code exists**

Run: `backend/.venv/bin/pytest backend/tests/test_group_ai_send_limits.py -k group_ai_send_respects_group_daily_limit -q`

Expected: FAIL because the existing dispatcher calls Telegram despite the consumed group daily limit.

- [x] **Step 3: Add a cooldown regression test**

```python
@pytest.mark.no_postgres
def test_group_ai_send_respects_group_cooldown(monkeypatch):
    # Seed a durable gateway-attempt slot inside group_cooldown_seconds.
    # Assert a second action is deferred before Telegram and reports SLOWMODE.
```

- [x] **Step 4: Run the cooldown test before production code exists**

Run: `backend/.venv/bin/pytest backend/tests/test_group_ai_send_limits.py -k group_ai_send_respects_group_cooldown -q`

Expected: FAIL because the existing dispatcher has no Task Center group cooldown gate.

### Task 2: Reserve a durable group send slot immediately before Telegram

**Files:**
- Modify: `backend/app/services/task_center/dispatcher.py:1347-1393`
- Test: `backend/tests/test_group_ai_send_limits.py`

- [x] **Step 1: Add a locked daily-slot helper**

```python
def _reserve_group_send_attempt(
    session: Session,
    action: Action,
    context: GroupSendGatewayContext,
) -> ExecutionAttempt | None:
    locked_group = session.scalar(select(TgGroup).where(TgGroup.id == context.group.id).with_for_update())
    failure_type, detail = _group_send_slot_failure(session, action, locked_group)
    if failure_type:
        _fail_group_ai_send_before_gateway(session, action, context.payload, failure_type, detail, auto_check="拦截", validation_stage="group_send_limit")
        return None
    attempt = _begin_execution_attempt(session, action, context.account)
    _mark_executing(action)
    session.commit()
    return attempt
```

The helper must count same-day `before_call`, `gateway_call_started`, `success`, and `result_unknown` attempts for the exact `group_id`; it must also include legacy `message_tasks` already marked sent. The most recent durable slot must enforce `group_cooldown_seconds`. Failed pre-gateway attempts do not consume a slot; unknown after gateway does. A cap or cooldown hit must defer the current Action to the calculated next eligible time rather than feed a misleading one-second generic Telegram retry loop.

- [x] **Step 2: Use the helper at the final gateway boundary**

```python
attempt = _reserve_group_send_attempt(session, action, context)
if attempt is None:
    return True
_mark_gateway_call_started(session, attempt)
result = gateway.send_message(...)
```

- [x] **Step 3: Run the focused regression tests**

Run: `backend/.venv/bin/pytest backend/tests/test_group_ai_send_limits.py -q`

Expected: PASS; both actions are deferred before the Telegram gateway.

### Task 3: Align the product design and run focused regression coverage

**Files:**
- Modify: `docs/03-feature-designs/ai-group-all-accounts-daily-coverage-prd.md`
- Test: `backend/tests/test_ai_group_daily_coverage_planner.py`
- Test: `backend/tests/test_task_daily_coverage_dispatch.py`
- Test: `backend/tests/test_task_center_capacity_dispatch.py`

- [x] **Step 1: Document the final runtime invariant**

```markdown
Before a Task Center AI group send invokes Telegram, it must durably reserve a group/day slot under a group-row lock. A slot is consumed by a gateway-started, successful, or unknown send; daily limit and cooldown are checked against those slots.
```

- [x] **Step 2: Run portable coverage and dispatcher regressions**

Run: `backend/.venv/bin/pytest backend/tests/test_group_ai_send_limits.py backend/tests/test_ai_group_daily_coverage_planner.py backend/tests/test_task_daily_coverage_dispatch.py backend/tests/test_task_center_capacity_dispatch.py backend/tests/test_task_center_dispatcher_target_permission.py backend/tests/test_operations_center_runtime.py -m no_postgres -q`

Expected: PASS with no failures. The local environment has no `TEST_DATABASE_URL`, so the PostgreSQL-required subset remains a release-workflow check, where the Deploy Production workflow supplies PostgreSQL 16.

- [x] **Step 3: Run static validation**

Run: `backend/.venv/bin/python -m py_compile backend/app/services/task_center/dispatcher.py && git diff --check`

Expected: exit 0.

### Task 4: Release and restore live capacity without bypassing safeguards

**Files:**
- Release from: `master -> release -> GitHub Actions Deploy Production`
- Production data: four target `tg_groups.daily_limit` values and one audit record per group

- [ ] **Step 1: Verify the release candidate’s commit, tests, and Actions deployment result**

Run: targeted tests, `git diff --check`, then the repository’s normal `master -> release` deployment path.

- [ ] **Step 2: Apply only the required live group-limit configuration through the audited service path**

```text
For each all-account task: set group.daily_limit >= 675 + currently reserved ordinary dialogue budget.
Do not alter account_coverage_mode, task selection, cooldown, or account denominator.
```

- [ ] **Step 3: Validate production with durable evidence**

```text
1. coverage_capacity_proof.sufficient=true for all four tasks;
2. current-day ledger has no artificial completion and records actual blockers;
3. action + execution_attempt remote_message_id counts advance for eligible accounts;
4. the natural Zhengzhou University task cannot create a 121st same-day successful group message after deployment;
5. worker heartbeats are fresh.
```

No completion claim is allowed until a full Beijing-day ledger proves all eligible obligations confirmed, with unresolved account-level blockers explicitly remaining in the denominator.
