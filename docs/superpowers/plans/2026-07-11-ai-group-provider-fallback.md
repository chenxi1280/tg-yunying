# AI Group Provider Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the approved production chain `MiniMax-M3 -> MiniMax-M2.5 -> Grok 4.5 CLI -> static safe check-in/emoji` with an English safe-context Prompt, Chinese exact JSON, explicit observability, configuration switches, and production verification.

**Architecture:** Reuse the existing three quality-generation rounds as three named provider stages so executor-level quality rejection can advance the chain without duplicating quality gates. A new safe Prompt module freezes sanitized group metadata/context; MiniMax stages use existing OpenAI-compatible Providers, Grok uses a bounded subprocess bridge, and the existing emoji fallback becomes the single static fallback path with additional safe check-in text. Provider/source metadata travels on `GeneratedContent` into `SendMessagePayload` and task stats.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/Alembic, Pydantic, MiniMax OpenAI-compatible API, Grok CLI 0.2.93, React/TypeScript, Docker Compose, pytest.

---

### Task 1: Add tenant fallback controls and dual MiniMax provider provisioning

**Files:**
- Modify: `backend/app/models/ai.py`
- Modify: `backend/app/schemas/ai_config.py`
- Modify: `backend/app/services/ai_config.py`
- Create: `backend/migrations/versions/0087_add_ai_group_fallback_settings.py`
- Modify: `frontend/src/app/types/content.ts`
- Modify: `frontend/src/app/AppModals.tsx`
- Modify: `frontend/src/app/views/AISettingsView.tsx`
- Modify: `.github/scripts/update_minimax_provider.py`
- Test: `backend/tests/test_ai_group_provider_fallback.py`
- Test: `backend/tests/test_update_minimax_provider_script.py`
- Test: `backend/tests/test_frontend_permission_gating.py`

- [ ] Add failing tests proving tenant settings expose `ai_group_model_fallback_enabled`, `ai_group_grok_fallback_enabled`, and `ai_group_static_fallback_enabled`, all defaulting to true.
- [ ] Add a failing deployment-script test proving one secret creates/updates separate `MiniMax-M3` and `MiniMax-M2.5` rows and points the tenant default to M3 without overwriting M2.5.
- [ ] Run the focused tests and confirm failures are caused by missing fields/dual-provider behavior.
- [ ] Add the migration, model/schema/service fields, TypeScript fields, three UI switches, and dual-provider upsert.
- [ ] Run focused backend/frontend source tests and commit.

### Task 2: Build the English safe-context Prompt

**Files:**
- Create: `backend/app/services/task_center/ai_group_prompt.py`
- Modify: `backend/app/services/task_center/ai_generator.py`
- Test: `backend/tests/test_ai_group_safe_prompt.py`

- [ ] Add failing tests for independent sanitization of group title, persona/profile, topic/teacher, reply target, and recent chat clauses.
- [ ] Add failing tests proving explicitly adult non-explicit phrases such as `身材曲线`, `腿又长又白`, `黑丝和高跟鞋`, `性感穿搭`, and `气质撩人` survive while price/contact/booking/service/explicit/minor phrases do not.
- [ ] Add failing tests proving instructions are English, safe dynamic data may remain Chinese, `safe_context` requires phrase reuse, `generic_warmup` is limited to greeting/check-in/weather/presence, and output has the exact fixed JSON keys for the requested draft count.
- [ ] Implement immutable prompt-input/result dataclasses, safe allowlists, neutral fallbacks, at-most-five recent safe messages, and prompt builders.
- [ ] Route group normal/reply generation through the new bundle while leaving channel comments and other AI tasks unchanged.
- [ ] Run tests and commit.

### Task 3: Map the three quality rounds to M3, M2.5, and Grok

**Files:**
- Modify: `backend/app/services/task_center/ai_generator.py`
- Modify: `backend/app/services/task_center/executors/group_ai_chat.py`
- Modify: `backend/app/services/task_center/payloads.py`
- Test: `backend/tests/test_ai_group_provider_fallback.py`
- Test: `backend/tests/test_group_ai_chat_dataflow.py`

- [ ] Add failing tests proving round 0 requests `MiniMax-M3`, round 1 requests `MiniMax-M2.5`, round 2 requests Grok, and a successful earlier round does not call later stages.
- [ ] Add failing tests proving technical errors, empty/refusal/JSON failures, and executor quality rejection advance to the next stage once, while disallowed input never invokes the provider chain.
- [ ] Extend `GeneratedContent` and `SendMessagePayload` with `requested_model`, `actual_model`, `fallback_stage`, `fallback_reason`, `provider_duration_ms`, and bounded `generation_attempts` summaries.
- [ ] Catch per-stage `AiGenerationUnavailable` inside the quality loop, record the stage failure, and continue without aborting the whole plan.
- [ ] Preserve the same frozen prompt bundle and remaining slots across stages; remove the old meaning of “three rewrites of the same model.”
- [ ] Run tests and commit.

### Task 4: Add the bounded Grok CLI bridge

**Files:**
- Create: `backend/app/services/grok_cli_bridge.py`
- Modify: `backend/app/config.py`
- Modify: `backend/app/ai_gateway.py`
- Modify: `docker-compose.server.yml`
- Modify: `.github/workflows/deploy-production.yml`
- Test: `backend/tests/test_grok_cli_bridge.py`
- Test: `backend/tests/test_worker_roles.py`

- [ ] Add failing unit tests for disabled/missing binary, shared lock capacity, hard timeout, nonzero exit, invalid CLI envelope, non-`EndTurn`, and valid draft parsing.
- [ ] Implement subprocess execution with argument arrays, a temporary Git directory, `--no-memory`, `--no-subagents`, `--disable-web-search`, `--permission-mode dontAsk`, fixed `grok-4.5`, JSON output, stderr truncation, and a shared file lock.
- [ ] Expose settings for enabled state, binary path, home/lock path, timeout, and model; never log Prompt, auth state, or secrets.
- [ ] Mount the authenticated Grok home into backend/planner/dispatcher containers and add deploy preflight checks for version/login/model.
- [ ] Parse Grok raw text through the same draft parser and common output gates.
- [ ] Run tests and commit.

### Task 5: Consolidate static fallback and runtime observability

**Files:**
- Modify: `backend/app/services/task_center/executors/group_ai_chat.py`
- Modify: `backend/app/services/task_center/payloads.py`
- Modify: `backend/app/services/operations_center.py`
- Modify: `backend/app/services/ai_group_quality_diagnostics.py`
- Test: `backend/tests/test_operations_center_runtime.py`
- Test: `backend/tests/test_ai_group_quality_diagnostics.py`
- Test: `backend/tests/test_group_ai_chat_dataflow.py`

- [ ] Add failing tests proving all three model stages exhausted yields a versioned safe check-in or existing `emoji_react`, marked `static_safe_fallback`, and the tenant switch disables it in favor of a visible skipped round.
- [ ] Add failing tests proving fallback content still passes duplicate, capacity, account, rule, and send gates.
- [ ] Add failing tests proving task/action projections and diagnostics distinguish `primary_m3`, `fallback_m25`, `fallback_grok`, `static_safe_fallback`, and all-stage failure.
- [ ] Replace the hard-hourly-only fallback gate with the tenant setting and merge the safe check-in pool with the existing low-risk emoji pool.
- [ ] Persist bounded attempt summaries and provider-source fields without storing raw reasoning, full stderr, keys, or login data.
- [ ] Run tests and commit.

### Task 6: Full QA, release, and production acceptance

**Files:**
- Modify: `docs/00-index/project-structure-index.md`
- Modify: `docs/00-index/project-dataflow-index.md`
- Modify: `docs/04-ops/deployment/PRODUCTION_RUNTIME.md`
- Modify: `docs/05-implementation/multi-agent-practice/agent-status-board.md`
- Create: `docs/05-implementation/multi-agent-practice/runs/2026-07-11-ai-group-provider-fallback-release.md`

- [ ] Run focused fallback, Prompt, Gateway, executor, diagnostics, compose, schema, and frontend tests with the backend venv and a 60-second bound per backend command.
- [ ] Run `py_compile`, `git diff --check`, frontend type/build checks, migration head checks, and credential scans.
- [ ] Update structure/dataflow/ops/run evidence and commit QA results.
- [ ] Merge the feature branch into local `master`, merge `master` into `release`, push `release`, and wait for `Deploy Production` to complete.
- [ ] SSH-verify two healthy MiniMax Provider rows, tenant default M3, six running tasks without stale overrides, Grok CLI availability inside the relevant containers, API/worker health, and no secret leakage.
- [ ] Execute controlled no-Telegram dry-runs for M3 success, forced M2.5 fallback, forced Grok fallback, and forced static fallback; verify exact source/reason/latency fields.
- [ ] Sample real production task actions only after normal scheduling and distinguish `deployed`, `dry_run_pass`, and `production_fixed` evidence.
