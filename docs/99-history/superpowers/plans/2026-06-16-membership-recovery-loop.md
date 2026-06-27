# Membership Recovery Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a system-level account admission recovery loop that classifies failed group-join accounts, automatically requeues recoverable accounts, exposes operator/administrator queues, and prevents unusable accounts from being treated as joinable capacity.

**Architecture:** Add a focused backend classifier for membership recovery state, feed it into task-center membership item payloads and summaries, then use that same classifier inside membership reactivation to requeue only auto-recoverable cases. Keep Telegram-side constraints explicit: group administrator restrictions remain manual and frozen/unavailable accounts are excluded from automatic join retries.

**Tech Stack:** FastAPI service layer, SQLAlchemy models, pytest, React/TypeScript task-center UI.

---

### Task 1: Recovery Classification

**Files:**
- Create: `backend/app/services/task_center/membership_recovery.py`
- Modify: `backend/app/services/task_center/details.py`
- Modify: `backend/app/schemas/task_center.py`
- Test: `backend/tests/test_task_center_membership_items.py`

- [x] **Step 1: Write failing tests**

Add tests that assert membership rows include:
- `recovery_bucket="auto_retry"` for required-channel failures.
- `recovery_bucket="verification"` for captcha verification.
- `recovery_bucket="group_admin"` for non-auto group restriction.
- `recovery_bucket="account_unavailable"` for frozen/unavailable accounts.

- [x] **Step 2: Verify red**

Run: isolated Python invocation of `test_membership_items_page_projects_recovery_queue_fields`
Observed: failed with `KeyError: 'recovery_bucket'`.

- [x] **Step 3: Implement classifier**

Create a pure helper that accepts action result, account status, and latest verification task, then returns stable queue fields:
- `recovery_bucket`
- `recovery_label`
- `recovery_action`
- `operator_required`
- `auto_retryable`
- `account_replace_required`

- [x] **Step 4: Wire detail payload and schema**

Add classifier output to membership item payloads and Pydantic/TypeScript types.

- [x] **Step 5: Verify green**

Run: isolated Python invocation of `test_membership_items_page_projects_recovery_queue_fields`
Observed: pass. Normal pytest collection is currently blocked by the configured PostgreSQL test database connection.

### Task 2: Automatic Requeue Scope

**Files:**
- Modify: `backend/app/services/task_center/channel_membership.py`
- Test: `backend/tests/test_channel_membership_strategy.py`

- [x] **Step 1: Write failing tests**

Add tests proving:
- Required-channel and auto-verification failures create a new pending membership action.
- Group-admin and account-unavailable failures do not create a retry action.

- [x] **Step 2: Verify red**

Run: isolated Python invocation of `test_reactivate_memberships_requeues_recoverable_failures_for_group_ai`
Observed: failed because no retry action was created.

- [x] **Step 3: Implement requeue decision**

Reuse `membership_recovery.py` so scheduling and detail UI use the same reason taxonomy.

- [x] **Step 4: Verify green**

Run: isolated Python invocation of both recovery requeue tests
Observed: pass.

### Task 3: Frontend Visibility

**Files:**
- Modify: `frontend/src/app/types/taskCenter.ts`
- Modify: `frontend/src/app/views/TaskMembershipPanel.tsx`

- [x] **Step 1: Add typed fields**

Expose recovery bucket/action fields in `TaskMembershipItem`.

- [x] **Step 2: Show operator-facing queue labels**

Add a compact recovery column and drawer rows so operators can distinguish auto retry, captcha, group admin, and account replacement.

- [x] **Step 3: Verify frontend build**

Run: `npm --prefix frontend run build`
Observed: build succeeds.

### Task 4: Final Verification

**Files:**
- All modified files

- [x] **Step 1: Run focused backend tests**

Run: isolated Python invocation of focused backend tests
Observed: pass. Normal pytest collection is currently blocked by the configured PostgreSQL test database connection.

- [x] **Step 2: Run syntax/static checks**

Run: `PYTHONPATH=backend python -m py_compile backend/app/services/task_center/membership_recovery.py backend/app/services/task_center/details.py backend/app/services/task_center/channel_membership.py backend/app/schemas/task_center.py`
Observed: no output, exit 0.

- [x] **Step 3: Inspect diff**

Run: `git diff --check`
Observed: no whitespace errors.
