# AI Active Group Quality Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the backend foundation for PRD-defined AI active group message memory, account voice profiles, stance memory, and account online keepalive.

**Architecture:** Add durable SQLAlchemy models and Alembic migration first, then small service modules that own dedupe reservation, voice profile lookup, stance update, and online-state reconcile. Planner and dispatcher consume these services through narrow interfaces instead of embedding SQL directly.

**Tech Stack:** FastAPI backend, SQLAlchemy ORM, Alembic migrations, pytest, existing task-center worker roles.

---

### Task 1: Account Online State Worker Foundation

**Files:**
- Modify: `backend/app/models/task_center.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/app/services/account_online_state.py`
- Modify: `backend/app/services/__init__.py`
- Modify: `backend/app/worker.py`
- Create: `backend/migrations/versions/0071_ai_group_quality_foundation.py`
- Test: `backend/tests/test_account_online_state.py`
- Test: `backend/tests/test_worker_roles.py`

- [x] **Step 1: Write failing tests**

Add tests proving:
- `reconcile_account_online_sources` creates one row per account and stores `desired_sources`, `active_task_count`, `session_kind`, `session_id`, `proxy_id`, and `stale_after_at`.
- Removing a task source clears orphaned `desired_online` when no other source remains.
- A stale online account is not considered dispatch-ready.
- `worker.drain_once(role="account-online")` calls the online keepalive drain and records a dedicated role.

- [x] **Step 2: Run tests to verify failure**

Run:

```bash
cd /Users/xida/PycharmProjects/tg-yunying
timeout 60s backend/.venv/bin/pytest -q backend/tests/test_account_online_state.py backend/tests/test_worker_roles.py -q
```

Expected: tests fail because `TgAccountOnlineState`, service functions, and `account-online` role do not exist.

- [x] **Step 3: Implement minimal model, migration, service, worker role**

Implement:
- `TgAccountOnlineState` model with unique `(tenant_id, account_id)`.
- Migration `0071_ai_group_quality_foundation.py`, down revision `0070_migrate_group_ai_topic_hint`.
- `reconcile_account_online_sources(session, tenant_id, sources, now=...)`.
- `is_account_online_ready(session, tenant_id, account_id, now=...)`.
- `drain_account_online_keepalive(session_factory, limit)` with heartbeat-safe no-op probing for now: it reconciles stale rows and records visible stale/offline state without Telegram-visible activity.
- Worker role `account-online`, healthcheck membership, CLI choices.

- [x] **Step 4: Run tests to verify pass**

Run the same pytest command and confirm pass.

### Task 2: AI Message Memory Reservation Foundation

**Files:**
- Modify: `backend/app/models/task_center.py`
- Create: `backend/app/services/task_center/ai_message_memory.py`
- Test: `backend/tests/test_ai_group_message_memory.py`

- [x] **Step 1: Write failing tests**

Add tests proving:
- Normalization treats whitespace, repeated punctuation, and cosmetic emoji changes consistently.
- A reserved message blocks another reservation with the same tenant/group/fingerprint inside the 5-minute window.
- `unknown_after_send` and `success` participate in duplicate checks.
- Expired pre-gateway reservations are marked visible instead of deleted.

- [x] **Step 2: Run tests to verify failure**

Run:

```bash
timeout 60s backend/.venv/bin/pytest -q backend/tests/test_ai_group_message_memory.py -q
```

Expected: fail because memory model and reservation service do not exist.

- [x] **Step 3: Implement model and reservation service**

Implement `AiGroupMessageMemory` model and service functions:
- `normalize_group_ai_text(text)`
- `reserve_group_ai_message(session, ...)`
- `mark_group_ai_message_result(session, memory_id, status, action_id=None, sent_at=None, result=None)`
- `expire_stale_group_ai_reservations(session, now=...)`

- [x] **Step 4: Run tests to verify pass**

Run the same pytest command and confirm pass.

### Task 3: Account Voice Profile And Stance Models

**Files:**
- Modify: `backend/app/models/task_center.py`
- Create: `backend/app/services/task_center/account_voice_profiles.py`
- Test: `backend/tests/test_ai_account_voice_profiles.py`

- [x] **Step 1: Write failing tests**

Add tests proving:
- Missing voice profiles are initialized deterministically for account IDs with non-generic summaries.
- Edited profile versions are read by next planning call, while action payloads can keep old versions.
- Stance memory upserts by tenant/group/account and preserves recent topic/teacher/act type.

- [x] **Step 2: Run tests to verify failure**

Run:

```bash
timeout 60s backend/.venv/bin/pytest -q backend/tests/test_ai_account_voice_profiles.py -q
```

Expected: fail because profile and stance models/services do not exist.

- [x] **Step 3: Implement profile and stance services**

Implement:
- `AiAccountVoiceProfile`
- `AiAccountGroupStanceMemory`
- `ensure_voice_profiles_for_accounts`
- `voice_profile_prompt_summaries`
- `upsert_group_stance_memory`
- `group_stance_summaries`

- [x] **Step 4: Run tests to verify pass**

Run the same pytest command and confirm pass.

### Task 4: Planner And Dispatcher Integration Guardrails

**Files:**
- Modify: `backend/app/services/task_center/executors/group_ai_chat.py`
- Modify: `backend/app/services/task_center/dispatcher.py`
- Test: `backend/tests/test_group_ai_chat_dataflow.py`
- Test: `backend/tests/test_task_center_capacity_dispatch.py`

- [x] **Step 1: Write failing tests**

Add tests proving:
- Planner skips offline/stale accounts and records `account_offline` instead of AI quality failure.
- Planner payload includes `slot_id`, `act_type`, voice profile version/summary, stance summary, and memory reservation ID.
- Dispatcher performs a final online check before sending and does not silently swap accounts.

- [x] **Step 2: Run tests to verify failure**

Run:

```bash
timeout 60s backend/.venv/bin/pytest -q backend/tests/test_group_ai_chat_dataflow.py backend/tests/test_task_center_capacity_dispatch.py -q
```

Expected: fail until integration points use the new services.

- [x] **Step 3: Implement integration**

Wire planner and dispatcher through the new service interfaces. Keep AI generation batch-based; do not add per-message AI calls in this task.

- [x] **Step 4: Run tests to verify pass**

Run the same pytest command and confirm pass.

Current evidence: full SQLite-safe no-postgres suite passed (`402 passed, 827 deselected`), frontend production build passed, `git diff --check` passed, Alembic single-head check reports `0071_ai_group_quality_foundation`, and targeted offline SQL generation for `0070_migrate_group_ai_topic_hint:head` produced the expected 0071 tables/indexes/version update. Follow-up omission review added real `account-online` health probing through `TelegramGateway.check_account_health`; `backend/tests/test_account_online_state.py` and `backend/tests/test_worker_roles.py` pass. Real PostgreSQL upgrade and production worker evidence remain release/QA gates because the configured test database closed connections unexpectedly and local Docker is not running.

### Remaining Implementation Gaps Found During Review

- [x] Add atomic database enforcement for `ai_group_message_memory.reservation_key` so concurrent planners cannot pass the 5-minute exact duplicate window through query-then-insert races.
- [x] Expand message memory from the current 5-minute exact normalized duplicate foundation to the PRD-required 1-hour high similarity, 7-day semantic hard dedupe, and 30-day template-shell rate limit.
- [x] Add migration / reconcile entry points for running AI active group tasks, relay tasks, listener sources, and global keepalive settings so `tg_account_online_state.desired_sources` is populated immediately after deploy.
- [x] Persist dispatcher success / unknown-send outcomes into account group stance memory so next planning cycles inherit the same account attitude instead of only reading pre-existing stance rows.
- [x] Implement the max-3-round generation loop and explicit unique-emoji fallback path.
- [x] Expose account voice profile management in system settings, including search, edit, rebuild, bulk initialize, rollback, audit, and missing-profile filters.
- [x] Add task detail quality funnel fields for duplicate hits, template-shell limit, voice-profile mismatch, stance conflict, online-state exclusion, context insufficiency, fallback emoji count, and AI call rounds.

### Remaining Verification Gaps

- [x] Run PostgreSQL-backed migration and integration tests for `0071_ai_group_quality_foundation.py`; GitHub Actions checks run the full backend suite against PostgreSQL/Redis, and the release deploy has applied the migration path on production without health regression.
- [x] Deploy through the standard `master -> release -> GitHub Actions Deploy Production` path; Deploy Production run `28356406443` succeeded for commit `83d01e95`.
- [x] Verify production `account-online` and `ai-memory` workers are running; Deploy Production run `28356406443` reported backend plus planner / dispatcher 1-4 / listener / recovery / account-security / account-online / ai-memory / metrics containers healthy.
- [x] Add a production diagnostics entry point that can prove AI 活群 expression-card, message-memory, duplicate-risk, quality-funnel-adjacent payload, and account-online evidence without manual SSH SQL (`run_ai_group_quality_diagnostics` -> `.github/scripts/ai_group_quality_diagnostics.py`).
- [ ] Run `run_ai_group_quality_diagnostics` on production and verify one real AI active group task shows non-duplicated send history, account online summary, AI quality funnel / rejection stats, and voice profile payload/audit evidence.
  - 2026-06-29 diagnostic run `28359742281` proved worker heartbeats, 30-day message-memory usage, topic config, and recent quality rejections, but exposed `active_profile_count=0` / `voice_profile_payload_count=0`.
  - 2026-06-29 production release `20260629112823_5ca5581` reached live health checks, but workflow run `28368475561` failed during `reconcile_account_profiles` because the real AI provider returned HTTP 429 `quota exhausted`. This is a real external quota blocker, not a code-path pass. The follow-up fix makes expression-card reconcile commit each small account batch independently and print structured partial progress before failing the release gate, so the next run can continue from remaining missing cards instead of rolling back all generated cards.
  - 2026-06-29 workflow run `28370437274` proved the partial-commit fix on production: 198 account expression cards were created and persisted, reducing missing cards from 442 to 244 before the gate failed on `AI 表达卡输出行字段数量错误`. The next fix changes the generation protocol to compact JSONL while retaining old pipe-line parser compatibility. Final checkbox remains open until production reconcile completes and `run_ai_group_quality_diagnostics` shows non-zero expression-card payload evidence.
  - 2026-06-29 workflow run `28371881406` proved compact JSONL code was live and created 34 more expression cards, reducing missing cards from 244 to 210, then failed explicitly on malformed JSON (`JSONDecodeError`) for batch `[285, 286]`. The next fix keeps the same real AI supplier but retries malformed multi-account structure output as single-account requests; a single-account malformed result still fails visibly.
  - 2026-06-29 production reconcile run `28373826998` completed the remaining expression-card initialization: missing voice profiles went from 210 to 0, active production accounts reached `active_profile_count=442`, `failed_batch_account_ids=[]`, and release `20260629130504_886573e` passed live backend/frontend health checks.
  - 2026-06-29 quality diagnostics run `28377346129` then proved workers, online summary, 30-day message memory, duplicate/recent-action checks, task topic counts, and `active_profile_count=442`, but exposed `voice_profile_payload_count=0` in sampled recent AI 活群 actions. Root cause: those actions were planned before expression cards existed. Follow-up fix skips still-open `pending/claiming` AI group send actions without `account_voice_profile_version` after the account now has an active expression card, marks the reserved message memory as `expired_before_send`, and lets the next planner cycle regenerate text with account expression-card prompt/payload evidence.
  - 2026-06-29 workflow run `28379774502` deployed the first replan cleanup fix and reran planner/quality diagnostics, but sampled tasks still showed `voice_profile_payload_count=0`. The run proved the missing piece: service-level open-action gating can skip `group_ai_chat.build_plan` before executor-local cleanup runs. The follow-up design moves cleanup to an executor `prepare_open_actions_for_planning` hook that service calls before `_open_actions_state`, so old profileless actions can be expired before the open-action skip decision.
  - 2026-06-29 workflow run `28380648304` proved the service prepare hook is on the production release path, but CI caught a PostgreSQL-only duplicate insert on `tg_account_online_state` because the prepare hook reconciled online sources and `build_plan` reconciled them again in the same transaction. The prepare hook is now intentionally read-only for online state: it reads existing online-ready accounts, expires old profileless actions, and leaves all online-source writes to the normal `build_plan` path.
  - 2026-06-29 diagnostic run `28382626492` proved the read-only prepare hook was deployed and old profileless reservations were being expired (`expired_before_send=1856`), but sampled tasks still had `voice_profile_payload_count=0`. Root cause: planner's open-action gate also treats `retryable_failed` as an open planning blocker, while the replan cleanup only expired `pending/claiming` profileless actions. The cleanup now includes `retryable_failed` profileless AI group actions so they cannot keep blocking `build_plan`.
  - 2026-06-29 diagnostic run `28384668742` proved open-action blockers were cleared (`open_action_counts={}`), 442/442 active accounts had expression cards, and recent duplicate checks had no repeated texts, but no new expression-card payload was generated because AI generation stopped with `没有健康小米 MiMo/mino 供应商`. Root cause: AI active group text generation still treated an empty task `ai_model` as `mimo-v2.5` and passed `required_model_family="mimo"`. The current fix makes empty `ai_model` use task `ai_provider_id` or tenant default healthy AI provider, while explicit MiMo/Mino models still require MiMo/Mino and keep same-family quota rotation.

### Review Notes: Known Boundaries To Verify In QA

- The 7-day duplicate guard uses deterministic normalized-text similarity rather than vector embeddings. It should catch exact, high-overlap, and common shell repeats, but deep paraphrase detection must be validated with production samples before claiming full semantic dedupe.
- Account voice profile initialization is explicit and auditable. Mock AI providers are rejected; initialization failures are recorded and surfaced through missing-profile management instead of silently creating generic cards.
- Emoji fallback is intentionally last resort after at most 3 generation rounds and must remain unique within a round. It should not be counted as high-quality text output in reporting.
