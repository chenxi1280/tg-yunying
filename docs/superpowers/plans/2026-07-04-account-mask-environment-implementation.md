# Account Mask Environment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the PRD update that moves account masks to a first-level menu, keeps global Clash configuration in system settings, and manages per-account proxy/fingerprint bindings by account, TG developer app, and authorization slot.

**Architecture:** Reuse the existing account voice profile, account authorization, and risk-control proxy resources. Add a small global Clash subscription model/API for system settings, extend account environment bindings with developer app scope, and expose a dedicated Account Masks view with tabs for masks, proxy bindings, fingerprints, and audit/anomaly summaries.

**Tech Stack:** FastAPI, SQLAlchemy/Alembic, Pydantic, React/Vite, Ant Design, pytest no_postgres contract tests.

---

### Task 1: Backend Data Contract

**Files:**
- Modify: `backend/app/models/search_join_environment.py`
- Modify: `backend/app/models/risk_control.py`
- Create: `backend/migrations/versions/0078_account_mask_environment_app_scope.py`
- Modify: `backend/tests/test_search_join_group_dataflow.py`

- [x] **Step 1: Write failing metadata tests**

Add assertions that `AccountEnvironmentBinding` and `FingerprintComboHistory` expose `developer_app_id` and `developer_app_api_id_snapshot`, and that global Clash tables exist in metadata.

- [x] **Step 2: Implement model and migration**

Add nullable developer app fields to environment binding/history, make active environment uniqueness include `tenant_id + account_id + developer_app_id + authorization_id + session_role`, and add global `proxy_airport_subscriptions` / `proxy_airport_nodes` tables.

- [x] **Step 3: Verify**

Run:

```bash
timeout 60 backend/.venv/bin/pytest backend/tests/test_search_join_group_dataflow.py -q
```

Expected: new tests pass.

### Task 2: Backend Services and APIs

**Files:**
- Create: `backend/app/schemas/account_environment.py`
- Create: `backend/app/services/account_environment.py`
- Create: `backend/app/services/proxy_airport_subscription.py`
- Modify: `backend/app/api/routers/system.py`
- Modify: `backend/app/api/routers/ai_config.py`
- Modify: `backend/app/auth.py`
- Modify: `backend/tests/test_account_environment_bindings.py`
- Modify: `backend/tests/test_system_actions_dataflow.py`
- Modify: `backend/tests/test_permission_vocabulary.py`

- [x] **Step 1: Write failing API/service tests**

Cover:
- `account_masks.view` and `account_environment.manage` are known permissions.
- `/api/proxy-airport-subscription` returns masked global subscription state and never returns full URL.
- PATCH saves encrypted URL and returns only masked preview.
- `/api/account-environment-bindings` projects account, authorization, developer app, proxy, configured fingerprint, and consistency status.
- PATCH stores proxy/fingerprint by `account_id + developer_app_id + authorization_id + session_role`.

- [x] **Step 2: Implement schemas/services/routes**

Keep writes explicit and auditable. Saving fingerprint metadata must return `pending_effect` and must not claim remote Telegram device metadata changed.

- [x] **Step 3: Verify**

Run:

```bash
timeout 60 backend/.venv/bin/pytest backend/tests/test_account_environment_bindings.py backend/tests/test_system_actions_dataflow.py backend/tests/test_permission_vocabulary.py -q
```

Expected: all pass.

### Task 3: Search Join Runtime Alignment

**Files:**
- Modify: `backend/app/services/client_metadata/bindings.py`
- Modify: `backend/app/services/task_center/executors/search_join_group.py`
- Modify: `backend/tests/test_search_join_group_executor.py`

- [x] **Step 1: Write failing runtime tests**

Cover that search_join environment selection includes developer app id/api id in the binding and returns the same metadata for the authorization slot.

- [x] **Step 2: Implement selection changes**

Derive developer app id/api id from `TgAccountAuthorization`. Existing generated metadata remains usable, but binding and duplicate checks become app-scoped.

- [x] **Step 3: Verify**

Run:

```bash
timeout 60 backend/.venv/bin/pytest backend/tests/test_search_join_group_executor.py -q
```

Expected: search_join runtime tests pass.

### Task 4: Frontend Navigation and Views

**Files:**
- Modify: `frontend/src/app/routes.ts`
- Modify: `frontend/src/app/utils.ts`
- Modify: `frontend/src/app/AppShell.tsx`
- Modify: `frontend/src/app/AppModals.tsx`
- Modify: `frontend/src/app/views/SystemConfigView.tsx`
- Create: `frontend/src/app/views/AccountMasksView.tsx`
- Create: `frontend/src/app/views/ProxyAirportSubscriptionView.tsx`
- Modify: `frontend/src/app/types/system.ts`
- Modify: `backend/tests/test_frontend_permission_gating.py`

- [x] **Step 1: Write failing frontend contract tests**

Assert:
- `accountMasks` has route `/account-masks`.
- shell nav contains first-level “账号面具”.
- system settings no longer embeds `AIAccountVoiceProfilesView`.
- system settings contains “Clash 配置”.
- Account Masks view contains tabs “面具管理 / 账号代理 / 授权指纹 / 异常与审计”.
- permission UI exposes `account_masks.view` and `account_environment.manage`.

- [x] **Step 2: Implement frontend**

Move voice profile view into Account Masks, add environment binding tables/forms, and add system Clash config tab with masked URL save/test/sync operations.

- [x] **Step 3: Verify**

Run:

```bash
timeout 60 backend/.venv/bin/pytest backend/tests/test_frontend_permission_gating.py -q
npm --prefix frontend run build
```

Expected: tests and build pass.

### Task 5: Indexes, Flow Records, and Release

**Files:**
- Modify: `docs/00-index/project-structure-index.md`
- Modify: `docs/05-implementation/multi-agent-practice/worklog/dev.md`
- Modify: `docs/05-implementation/multi-agent-practice/worklog/qa.md`
- Modify: `docs/05-implementation/multi-agent-practice/agent-status-board.md`
- Create: `docs/05-implementation/multi-agent-practice/runs/2026-07-04-account-mask-environment-release.md`

- [x] **Step 1: Update implementation indexes and worklogs**

Record changed entrypoints, modules, API paths, models, migrations, and validation evidence.

- [x] **Step 2: Run broad verification**

Run targeted backend tests, frontend build, compile checks, and `git diff --check`.

- [x] **Step 3: Subagent supervision**

Ask one read-only subagent to audit PRD coverage and one read-only subagent to review code quality. Fix actionable issues before release.

- [ ] **Step 4: Release**

Follow the project path `master -> release -> GitHub Actions Deploy Production`, then verify the deployed production `/api/health` and frontend route availability.
