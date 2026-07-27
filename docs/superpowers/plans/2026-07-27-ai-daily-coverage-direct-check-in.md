# AI Daily Coverage Direct Check-In Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every non-reply all-account daily-coverage send use the exact text `签到` without calling an AI provider.

**Architecture:** Detect a bound daily-coverage Action before the deferred AI generation path. Persist an explicit direct-check-in payload and a coverage-scoped message-memory reservation, while retaining admission, rotation, outbound-policy, Telegram Gateway, coverage-ledger, and remote-success gates. Normal AI discussion and reply Actions remain unchanged.

**Tech Stack:** Python, SQLAlchemy, pytest, PostgreSQL/SQLite-compatible models, GitHub Actions production release.

---

### Task 1: Product contract

**Files:**
- Modify: `docs/01-product/tg-ops-platform-prd.md`
- Modify: `docs/03-feature-designs/ai-conversation-humanization-and-group-bot-admission-prd.md`
- Modify: `docs/03-feature-designs/ai-group-daily-fulfillment-remediation-prd.md`
- Modify: `docs/00-index/project-dataflow-index.md`

- [ ] **Step 1: Replace the fallback-only daily-coverage rule**

Document that a non-reply Action with `coverage_ledger_id` uses exact `签到` as its primary content and does not enter M3, M2.5, Grok, prompt rewriting, or AI semantic-quality processing.

- [ ] **Step 2: Preserve operational gates**

Document that admission, speaker rotation, outbound policy, group pacing, account binding, coverage ledger, ExecutionAttempt, and non-empty `remote_message_id` remain mandatory.

- [ ] **Step 3: Define audit fields**

Require `act_type=check_in`, `generation_source=direct_check_in`, `content_source=check_in_direct`, `human_quality_decision=direct_check_in`, and `ai_generation_tokens=0`.

### Task 2: Failing end-to-end regression

**Files:**
- Modify: `backend/tests/test_ai_generation_phase_boundaries.py`

- [ ] **Step 1: Write the failing test**

```python
def test_daily_coverage_sends_exact_check_in_without_ai_provider(monkeypatch):
    actions, coverages = seed_reserved_normal_batch(session, _now())
    dispatch_action(..., normal_generator=forbidden_external)
    assert actions[0].payload["message_text"] == "签到"
    assert actions[0].payload["generation_source"] == "direct_check_in"
    assert observed["gateway_calls"] == 1
```

- [ ] **Step 2: Verify RED**

Run:

```bash
timeout 60 backend/.venv/bin/pytest -q backend/tests/test_ai_generation_phase_boundaries.py::test_daily_coverage_sends_exact_check_in_without_ai_provider
```

Expected: FAIL because the current implementation calls the forbidden AI provider.

### Task 3: Direct check-in preparation

**Files:**
- Create: `backend/app/services/task_center/direct_check_in.py`
- Modify: `backend/app/services/task_center/ai_generation_dispatch.py`
- Modify: `backend/app/services/task_center/dispatcher.py`
- Modify: `backend/app/services/task_center/executors/group_ai_chat.py`
- Modify: `backend/tests/test_task_center_capacity_dispatch.py`

- [ ] **Step 1: Add the direct-check-in predicate**

```python
def requires_direct_check_in(payload) -> bool:
    return bool(payload.coverage_ledger_id and not payload.reply_to_message_id)
```

- [ ] **Step 2: Persist exact content before AI generation**

Set `message_text="签到"`, `ai_generation_status="ready"`, zero AI tokens, and the direct audit fields. Reserve message memory with a key scoped to the coverage obligation and current Action so a released/replanned obligation gets a fresh binding without colliding with AI-text dedupe.

- [ ] **Step 3: Keep pre-send memory integrity**

Require the direct-check-in memory ID before Gateway, but do not run ordinary similarity rejection against other direct coverage check-ins.

- [ ] **Step 4: Remove the AI-mask planning dependency**

Allow online, sendable daily-coverage accounts without an active voice profile to enter the direct-check-in plan; retain the voice-profile gate for replies and non-coverage AI discussion.

- [ ] **Step 5: Verify GREEN**

Run the test from Task 2 and expect PASS.

### Task 4: Regression and release proof

**Files:**
- Test: `backend/tests/test_ai_generation_phase_boundaries.py`
- Test: `backend/tests/test_ai_generation_quality_pipeline.py`
- Test: `backend/tests/test_task_daily_coverage_dispatch.py`

- [ ] **Step 1: Run targeted regression**

```bash
timeout 60 backend/.venv/bin/pytest -q -m no_postgres \
  backend/tests/test_ai_generation_phase_boundaries.py \
  backend/tests/test_ai_generation_quality_pipeline.py \
  backend/tests/test_task_daily_coverage_dispatch.py
```

- [ ] **Step 2: Run syntax and diff checks**

```bash
backend/.venv/bin/python -m py_compile \
  backend/app/services/task_center/direct_check_in.py \
  backend/app/services/task_center/ai_generation_dispatch.py
git diff --check
```

- [ ] **Step 3: Release and production validation**

Commit only the scoped files, push through `master -> release`, require GitHub Actions success, then verify new successful daily-coverage Actions have exact text `签到`, direct audit fields, `ai_generation_tokens=0`, successful ExecutionAttempt, and non-empty `remote_message_id`.
