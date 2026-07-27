# Remove Mask Anchor Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop account-mask quality checks from modifying generated message text and prevent previously rewritten, unsent messages from reaching Telegram.

**Architecture:** Account masks remain generation style inputs and post-generation pass/reject checks. The quality pipeline preserves provider output through the mask gate; an incompatible candidate is rejected and only that slot may enter the existing bounded generation stages. A small legacy-rewrite module expires old open Actions marked `voice_profile_anchor_rewritten=true`, releases coverage reservations, and expires their message-memory reservations before planning or Gateway.

**Tech Stack:** Python, SQLAlchemy, pytest, PostgreSQL/SQLite-compatible queries, GitHub Actions production release.

---

### Task 1: Product and data-flow contract

**Files:**
- Modify: `docs/01-product/tg-ops-platform-prd.md`
- Modify: `docs/03-feature-designs/ai-conversation-humanization-and-group-bot-admission-prd.md`
- Modify: `docs/00-index/project-dataflow-index.md`
- Modify: `docs/00-index/project-structure-index.md`

- [ ] **Step 1:** Document that masks constrain tone, length, emoji use, and stance; topic and real context determine subject matter.
- [ ] **Step 2:** Document that mask quality returns pass/reject only; accepted provider text cannot be truncated or receive a fixed suffix.
- [ ] **Step 3:** Document `voice_profile_anchor_replan` cleanup for historical rewritten open Actions, message memory, and coverage.

### Task 2: Failing quality and prompt regressions

**Files:**
- Modify: `backend/tests/test_ai_generation_quality_pipeline.py`
- Modify: `backend/tests/test_operations_center_runtime.py`

- [ ] **Step 1: Replace the rewrite expectation**

```python
def test_voice_profile_never_rewrites_generated_content():
    result = run_quality("今天先聊聊", account_profile="男性老哥夜场表达")
    assert result.content == "今天先聊聊"
    assert result.voice_profile_anchor_rewritten is False
```

- [ ] **Step 2:** Assert a mask containing price/location preferences does not add mandatory transaction guidance and does not change a safe provider candidate.
- [ ] **Step 3:** Run both exact tests under 60 seconds and observe failures caused by the current rewrite and guidance.

### Task 3: Failing legacy Action cleanup regressions

**Files:**
- Create: `backend/tests/test_legacy_anchor_rewrite.py`
- Modify: `backend/tests/test_task_center_capacity_dispatch.py`

- [ ] **Step 1:** Test planner cleanup skips historical rewritten open Actions, expires memory, releases coverage, and increments cleanup stats.
- [ ] **Step 2:** Test Dispatcher blocks a claimed historical rewritten Action before Telegram and records `voice_profile_anchor_replan`.
- [ ] **Step 3:** Run both tests and observe failure because cleanup does not exist.

### Task 4: Minimal implementation

**Files:**
- Create: `backend/app/services/task_center/legacy_anchor_rewrite.py`
- Modify: `backend/app/services/task_center/ai_generation_pipeline.py`
- Modify: `backend/app/services/task_center/dispatcher.py`
- Modify: `backend/app/services/task_center/executors/group_ai_chat.py`

- [ ] **Step 1:** Pass original generated content into mask matching and keep `voice_profile_anchor_rewritten=False`.
- [ ] **Step 2:** Delete fixed suffixes, 12-character truncation, mandatory theme guidance, and theme-anchor mismatch; retain style checks.
- [ ] **Step 3:** Add bounded Planner cleanup and a pre-Gateway gate that expire legacy Actions without calling Telegram.
- [ ] **Step 4:** Re-run Tasks 2 and 3 and require GREEN.

### Task 5: Regression and release proof

**Files:**
- Test: `backend/tests/test_ai_generation_quality_pipeline.py`
- Test: `backend/tests/test_ai_generation_phase_boundaries.py`
- Test: `backend/tests/test_ai_generation_observability.py`
- Test: `backend/tests/test_operations_center_runtime.py`
- Test: `backend/tests/test_task_center_capacity_dispatch.py`

- [ ] **Step 1:** Run affected tests under a 60-second subprocess timeout with zero failures.
- [ ] **Step 2:** Compile changed Python, run `git diff --check`, and confirm removed rewrite symbols have no production references.
- [ ] **Step 3:** Commit, fast-forward `release`, require Deploy Production success, confirm the new image, and query for zero new rewrite facts and zero unsent legacy rewritten Actions.
