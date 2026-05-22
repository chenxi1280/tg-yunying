# AI Chat Quality Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make AI active-group planning prefer real-context replies, allow only controlled low-frequency warmup, and block repeated or unanchored fabricated experience claims.

**Architecture:** Keep the first implementation inside `backend/app/services/task_center/executors/group_ai_chat.py` because that file already owns context selection, idle continuation, repeated-message filtering, and send-action payload construction. Add small pure helpers for chat quality decisions so existing planner tests can cover them without new services or dependencies.

**Tech Stack:** Python 3, SQLAlchemy ORM models, pytest, existing task-center planner tests.

---

### Task 1: Regression Tests For Quality Gate

**Files:**
- Modify: `backend/tests/test_operations_center_runtime.py`

- [ ] **Step 1: Write failing tests**

Add tests near the existing group AI chat idle-continuation tests:

```python
def test_group_ai_chat_blocks_unanchored_idle_experience_claims(monkeypatch):
    # Given an idle-continuation round where AI invents unsupported concrete facts,
    # the planner should create no action and record the hallucination skip reason.
```

```python
def test_group_ai_chat_semantic_clusters_drop_repeated_experience_templates(monkeypatch):
    # Given candidates repeating photo/attitude/location/revisit clusters,
    # the planner should keep only distinct semantic clusters.
```

- [ ] **Step 2: Verify tests fail**

Run:

```bash
cd backend && pytest tests/test_operations_center_runtime.py::test_group_ai_chat_blocks_unanchored_idle_experience_claims tests/test_operations_center_runtime.py::test_group_ai_chat_semantic_clusters_drop_repeated_experience_templates -q
```

Expected: tests fail because `skip_reason`/semantic quality gate is not implemented.

### Task 2: Minimal Quality Gate

**Files:**
- Modify: `backend/app/services/task_center/executors/group_ai_chat.py`

- [ ] **Step 1: Implement pure helpers**

Add helpers for:

- AI chat mode names: `reply`, `idle_warmup`, `bootstrap`, `waiting_new_context`.
- Semantic cluster classification for repeated expressions such as photo-real, stable-attitude, early-location, revisit-feedback.
- Unanchored fact detection in idle warmup when text claims concrete experience without a recent human context or material anchor.
- Quality skip stats on task: `chat_mode`, `skip_reason`, `duplicate_risk`, `hallucination_risk`.

- [ ] **Step 2: Wire helpers into `build_plan`**

Run generated candidates through the quality gate after existing noise and similarity filtering, before send actions are created.

- [ ] **Step 3: Add action payload trace fields**

Add `chat_mode`, `anchor_message_ids`, `semantic_cluster`, `duplicate_risk`, `hallucination_risk`, and `quality_skip_reason` where relevant.

### Task 3: Verification

**Files:**
- Test: `backend/tests/test_operations_center_runtime.py`

- [ ] **Step 1: Run targeted tests**

```bash
cd backend && pytest tests/test_operations_center_runtime.py::test_group_ai_chat_blocks_unanchored_idle_experience_claims tests/test_operations_center_runtime.py::test_group_ai_chat_semantic_clusters_drop_repeated_experience_templates tests/test_operations_center_runtime.py::test_group_ai_chat_idle_continuation_generates_after_interval tests/test_operations_center_runtime.py::test_group_ai_chat_waits_when_no_new_real_context -q
```

- [ ] **Step 2: Run diff whitespace check**

```bash
git diff --check -- backend/app/services/task_center/executors/group_ai_chat.py backend/tests/test_operations_center_runtime.py docs/tg-ops-platform.md docs/tg-ops-platform-prd.md
```
