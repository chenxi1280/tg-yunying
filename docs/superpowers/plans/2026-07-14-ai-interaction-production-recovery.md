# AI Interaction Production Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all group-chat and channel-comment AI generation out of Planner transactions, keep worker health fresh during long drains, and release the verified fix through the production gate.

**Architecture:** Planner creates stable pending Action blueprints using only persisted facts. Dispatcher claims and commits, performs AI work with no active database transaction, CAS-persists ready/failed/unknown generation results, then enters the existing no-transaction Telegram gateway and short finalize path. A periodic worker heartbeat refreshes both database and local Docker health facts.

**Tech Stack:** Python 3.11, SQLAlchemy 2, Pydantic 2, pytest, PostgreSQL, Docker Compose, GitHub Actions.

---

### Task 1: Stabilize the existing group-chat Phase A/B/C implementation

**Files:**
- Modify: `backend/app/services/task_center/ai_generation_dispatch.py`
- Modify: `backend/app/services/task_center/ai_generation_*.py`
- Modify: `backend/app/services/task_center/dispatcher.py`
- Modify: `backend/app/services/task_center/executors/group_ai_chat.py`
- Test: `backend/tests/test_ai_generation_phase_boundaries.py`
- Test: `backend/tests/test_ai_generation_commit_cas.py`
- Test: `backend/tests/test_ai_generation_commit_recovery_postgres.py`
- Test: `backend/tests/test_real_planner_boundaries_postgres.py`
- Test: `backend/tests/test_task_recovery_backpressure.py`

- [x] **Step 1: Verify the boundary tests exercise real failures**

Keep assertions that Provider/Grok/Telegram callbacks observe no active transaction and that stale generation leases cannot write:

```python
def provider(*_args, **_kwargs):
    assert session.in_transaction() is False
    return generated

assert stale_action.payload["ai_generation_status"] != "ready"
```

- [x] **Step 2: Run the focused suite**

Run with a hard 60-second process timeout:

```bash
/Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/python -c 'import subprocess,sys; p=subprocess.run(sys.argv[1:], timeout=60); raise SystemExit(p.returncode)' /Users/xida/PycharmProjects/tg-yunying/backend/.venv/bin/pytest -q backend/tests/test_ai_generation_phase_boundaries.py backend/tests/test_ai_generation_commit_cas.py backend/tests/test_ai_generation_material_policy.py backend/tests/test_ai_generation_observability.py backend/tests/test_ai_generation_quality_pipeline.py backend/tests/test_dispatcher_dataflow.py
```

Expected: all selected tests pass; any failure is fixed before proceeding.

- [x] **Step 3: Keep new modules within hard limits**

Split `ai_generation_dispatch.py` if it remains above 500 lines. The public entry stays:

```python
def ensure_send_message_content(... ) -> SendMessagePayload:
    ...
```

Move reply/context loading or result persistence to a focused module; do not add compatibility fallback to Planner generation.

- [x] **Step 4: Write and run RED recovery-fencing tests**

Cover the actual recovery path instead of manually changing an Action back to executing:

```python
recovered = recover_stale_executing_actions(session)
reclaimed = claim_actions(session, worker_id="new-worker")

assert recovered == 1
assert reclaimed[0].id == action.id
assert reclaimed[0].payload["ai_generation_claim_token"] == reclaimed[0].claim_token
assert reclaimed[0].payload["ai_generation_claim_token"] != old_claim_token
assert provider.call_count == 1
```

The stale worker CAS must update zero rows. A `generating` Action that has not entered Gateway returns to recoverable generation state on the same Action/slot/coverage and is never labeled `unknown_after_send`.

- [x] **Step 5: Remove the hot Task write from Phase C**

Write a failing SQL/update observation test, then remove `_record_quality_audit()` task mutation from generation persistence. Generation attempt writes Action/audit facts only; metrics remains the task summary owner.

- [x] **Step 6: Run real PostgreSQL cases**

Run the commit-recovery, dual-claim, 20-slot batch and 10/30/60 mapping cases. Expected: no duplicate Action/reservation/provider call and each measured DB stage is below five seconds.

- [x] **Step 7: Commit the reviewed group-chat phase**

```bash
git add backend/app/services/task_center/ai_generation_*.py backend/app/services/task_center/ai_generator.py backend/app/services/task_center/dispatcher.py backend/app/services/task_center/executors/group_ai_chat.py backend/app/services/task_center/payloads.py backend/app/services/task_center/stats.py backend/tests/test_ai_generation_*.py backend/tests/test_ai_group_*.py backend/tests/test_dispatcher_dataflow.py backend/tests/test_group_ai_chat_dataflow.py backend/tests/test_operations_center_runtime.py backend/tests/test_real_planner_boundaries_postgres.py backend/tests/test_rule_center_refactor.py backend/tests/test_task_account_scope_sync.py backend/tests/test_task_center_capacity_dispatch.py backend/tests/test_task_daily_coverage_dispatch.py
git commit -m "fix: move group AI generation outside planner transactions"
```

### Task 2: Make channel-comment Planner create stable pending blueprints

**Files:**
- Modify: `backend/app/services/task_center/executors/channel_comment.py`
- Modify: `backend/app/services/task_center/payloads.py`
- Test: `backend/tests/test_channel_comment_generation_phases.py`
- Test: `backend/tests/test_ai_task_limits.py`

- [x] **Step 1: Write the failing Planner test**

```python
def test_channel_comment_planner_does_not_call_ai(session, seeded_task, monkeypatch):
    monkeypatch.setattr(channel_comment, "generate_channel_comments", lambda *_a, **_k: pytest.fail("Planner called AI"))
    monkeypatch.setattr(channel_comment, "generate_channel_reply_comments", lambda *_a, **_k: pytest.fail("Planner called AI"))

    created = channel_comment.build_plan(session, seeded_task)

    actions = list(session.scalars(select(Action).where(Action.task_id == seeded_task.id)))
    assert created == len(actions) > 0
    assert all(action.payload["ai_generation_status"] == "pending" for action in actions)
    assert all(action.payload["comment_text"] == "" for action in actions)
```

- [x] **Step 2: Run RED**

Expected: failure because Planner currently calls the mocked generators or payload validation rejects an empty comment.

- [x] **Step 3: Implement the minimum pending payload contract**

Add generation fields to `PostCommentPayload`, allow empty text only for pending/generating/persist-unknown, and make Planner persist the stable generation snapshot:

```python
class PostCommentPayload(ViewMessagePayload):
    comment_text: str = ""
    ai_generation_status: str = ""
    ai_generation_attempt_id: str = ""
    ai_generation_request_id: str = ""
    ai_generation_claim_owner: str = ""
    ai_generation_claim_token: str = ""

    @model_validator(mode="after")
    def require_comment_text(self):
        if not self.comment_text.strip() and self.ai_generation_status not in PENDING_GENERATION_STATUSES:
            raise ValueError("post_comment action requires comment_text unless AI generation is pending")
        return self
```

Planner computes direct/reply slots and account scheduling but never calls AI. Make the post-comment dedupe key independent of `comment_text` and generation audit fields.

- [x] **Step 4: Run GREEN and the lifetime-cap regressions**

Expected: Planner test and channel comment budget/lifetime-cap tests pass; `completed/next_run_at=null` remains stable.

- [x] **Step 5: Commit**

```bash
git add backend/app/services/task_center/executors/channel_comment.py backend/app/services/task_center/payloads.py backend/tests/test_channel_comment_generation_phases.py backend/tests/test_ai_task_limits.py
git commit -m "fix: defer channel comment AI generation to dispatcher"
```

### Task 3: Generate and CAS-persist channel comments in Dispatcher

**Files:**
- Create: `backend/app/services/task_center/comment_generation_dispatch.py`
- Modify: `backend/app/services/task_center/dispatcher.py`
- Test: `backend/tests/test_channel_comment_generation_phases.py`
- Test: `backend/tests/test_channel_comment_generation_postgres.py`

- [x] **Step 1: Write RED tests for direct, reply and unknown persistence**

```python
def provider(*_args, **_kwargs):
    assert session.in_transaction() is False
    return ["真实评论"]

dispatch_action(session, action, generation_dependencies=dependencies(provider=provider))
assert action.payload["ai_generation_status"] == "ready"
assert action.payload["comment_text"] == "真实评论"
```

Add a reply-target-invalid case that asserts no Provider/Gateway call and no direct-comment downgrade. Add an injected Phase C commit failure that asserts `ai_result_persist_unknown`, same Action id, and no Gateway call.

- [x] **Step 2: Run RED**

Expected: validation or empty-comment dispatch fails because no comment generation dispatcher exists.

- [x] **Step 3: Implement claim -> no-transaction AI -> CAS ready**

Expose one focused entry:

```python
def ensure_post_comment_content(session, action, account, payload, *, dependencies) -> PostCommentPayload:
    request = prepare_comment_generation_request(session, action, account, payload)
    session.commit()
    assert not session.in_transaction()
    result = generate_comment_result(request, dependencies)
    persist_comment_generation_result(session, request, result)
    session.commit()
    return PostCommentPayload.model_validate(session.get(Action, action.id).payload)
```

Use claim owner/token plus generation attempt id in the update condition. Generation failure is `generation_failed`; Phase C uncertainty is `ai_result_persist_unknown`; only Telegram boundary uncertainty is `unknown_after_send`.

- [x] **Step 4: Run GREEN plus dual-Dispatcher PostgreSQL tests**

Expected: one Action, one generation claim/provider call, no deadlock, no cross-tenant claim, direct/reply both ready before Gateway.

- [x] **Step 5: Commit**

```bash
git add backend/app/services/task_center/comment_generation_dispatch.py backend/app/services/task_center/dispatcher.py backend/tests/test_channel_comment_generation_phases.py backend/tests/test_channel_comment_generation_postgres.py
git commit -m "fix: generate channel comments outside database transactions"
```

### Task 4: Refresh Docker health during long worker drains

**Files:**
- Modify: `backend/app/worker.py`
- Test: `backend/tests/test_worker_roles.py`

- [x] **Step 1: Write the failing periodic local-heartbeat test**

```python
def test_periodic_heartbeat_refreshes_db_and_local_file(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "_record_loop_heartbeat", lambda *_a: calls.append("db"))
    monkeypatch.setattr(worker, "_write_local_healthcheck_heartbeat", lambda: calls.append("local"))
    stop = StopAfterFirstWait()

    worker._periodic_heartbeat_loop("dispatcher", 100, stop)

    assert calls == ["db", "local"]
```

- [x] **Step 2: Run RED**

Expected: only `db` is recorded.

- [x] **Step 3: Add the local refresh to the existing periodic loop**

```python
def _refresh_worker_heartbeat(role: str, limit: int) -> None:
    _record_loop_heartbeat(role, limit)
    _write_local_healthcheck_heartbeat()
```

Call this helper from both the main loop and periodic thread; surface write errors in logs.

- [x] **Step 4: Run GREEN**

Run `backend/tests/test_worker_roles.py` under 60 seconds. Expected: all worker role and healthcheck tests pass.

- [x] **Step 5: Commit**

```bash
git add backend/app/worker.py backend/tests/test_worker_roles.py
git commit -m "fix: keep worker local health fresh during long drains"
```

### Task 5: Integration QA, indexes and release gate

**Files:**
- Modify: `docs/01-product/tg-ops-platform-prd.md`
- Modify: `docs/00-index/project-structure-index.md`
- Modify: `docs/05-implementation/multi-agent-practice/runs/2026-07-13-ai-group-planner-scale-fix.md`
- Modify: `docs/superpowers/plans/2026-07-14-ai-interaction-production-recovery.md`
- Verify: backend focused and PostgreSQL suites

- [x] **Step 1: Run the complete affected backend suite with per-command 60-second timeouts**

Include group generation, comment generation, dispatcher, coverage, lifetime cap, recovery, worker roles, stats and real PostgreSQL boundary tests. Expected: zero failures and no warnings that hide rollback/connection leaks.

- [x] **Step 2: Run static hygiene**

```bash
git diff --check
ruff check backend/app/services/task_center backend/app/worker.py backend/tests
```

Expected: exit 0. Confirm every new function is at most 50 lines, new file at most 500 lines, nesting at most three and positional parameters at most three.

- [x] **Step 3: Update structure and run records**

Record the new Planner/Dispatcher module boundaries and E2 evidence. Keep Product Acceptance and production recovery distinct.

- [x] **Step 4: Independent QA and Product Acceptance**

QA checks every E2 item from the专项设计. Product then verifies all original requirements are covered; `qa_pass` alone does not change production status.

- [x] **Step 5: Commit QA/index evidence**

```bash
git add docs/01-product/tg-ops-platform-prd.md docs/00-index/project-structure-index.md docs/05-implementation/multi-agent-practice/runs/2026-07-13-ai-group-planner-scale-fix.md docs/superpowers/plans/2026-07-14-ai-interaction-production-recovery.md
git commit -m "docs: record final E2 acceptance"
```

### Task 6: Release and production E4

**Files:**
- Release path: `master -> release -> GitHub Actions Deploy Production`
- Evidence: production SSH, PostgreSQL and Telegram success facts

- [ ] **Step 1: Merge reviewed commits to master and then release**

Do not copy code directly to the server. Verify exact SHAs before push and deployment.

- [ ] **Step 2: Deploy with a bounded worker restart**

Stop planner/dispatcher, clear only proven stale old-version DB sessions/claims with no remote side effect, deploy, then start planner -> dispatcher -> metrics/recovery. Do not resume paused Qingdao tasks or change Zhengzhou scope.

- [ ] **Step 3: Verify three complete cycles**

Require all relevant workers healthy, zero new deadlocks, no transaction over 60 seconds and no persistent lock waiter over five seconds.

- [ ] **Step 4: Verify business outcomes**

Group coverage numerator must increase against the approved dynamic denominator. Channel-comment overdue must reach zero or each row must have a reproducible blocker, and at least one new success Action must have a successful ExecutionAttempt and non-empty remote message id.

- [ ] **Step 5: Mark production status from evidence**

Only E4 evidence can set `production_fixed`; otherwise report `blocked` or `unproven` with the exact remaining condition.
