# Account Authorization Assets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the new account-center relogin foundation: multiple developer apps remain the capacity pool, while each TG account exposes a primary authorization asset and standby coverage without breaking current single-session accounts.

**Architecture:** Add a `tg_account_authorizations` model and migration, then provide a compatibility service that projects existing `tg_accounts.developer_app_id + proxy_id + session_ciphertext` as a primary authorization when no explicit row exists. Surface an authorization summary on account list/detail responses so the UI can warn when no standby session exists without blocking current capabilities.

**Tech Stack:** FastAPI, SQLAlchemy, Alembic, Pydantic, React, TypeScript, Ant Design.

---

### Task 1: Backend Authorization Asset Projection

**Files:**
- Modify: `backend/app/models/accounts.py`
- Modify: `backend/app/models/__init__.py`
- Modify: `backend/app/schemas/accounts.py`
- Modify: `backend/app/api/response_permissions.py`
- Create: `backend/app/services/account_authorizations.py`
- Create: `backend/migrations/versions/0055_account_authorizations.py`
- Test: `backend/tests/test_account_authorizations.py`

- [ ] **Step 1: Write failing backend tests**

Create tests that seed one legacy account with `developer_app_id`, `proxy_id`, and `session_ciphertext`, then assert the account API summary reports `primary_status="active"`, `standby_count=0`, `has_standby=false`, and `risk_hint` as a warning only.

Run: `cd backend && timeout 60 pytest tests/test_account_authorizations.py -q`
Expected: FAIL because the new service/schema fields do not exist.

- [ ] **Step 2: Add model and migration**

Create `TgAccountAuthorization` with `role`, `developer_app_id`, `proxy_id`, encrypted `session_ciphertext`, health fields, `is_current`, switch timestamps, and disabled fields. The migration must create indexes for account lookup and a partial unique index for current authorization per account where supported.

- [ ] **Step 3: Add projection service**

Implement `authorization_summary_for_account(session, account)` and `authorization_summaries_for_accounts(session, accounts)`. If explicit rows exist, summarize them. If none exist, project the existing account fields as the primary authorization and mark missing standby as non-blocking.

- [ ] **Step 4: Expose summary in account responses**

Extend `AccountOut` and `account_out_for_user()` with an `authorization_summary` object. Hide developer app/proxy internals according to existing sensitive permissions, but keep the non-sensitive warning text visible to account viewers.

- [ ] **Step 5: Verify backend tests**

Run: `cd backend && timeout 60 pytest tests/test_account_authorizations.py -q`
Expected: PASS.

### Task 2: Account Center Standby Warning UI

**Files:**
- Modify: `frontend/src/app/types/accounts.ts`
- Modify: `frontend/src/app/views/AccountsView.tsx`

- [ ] **Step 1: Add frontend type fields**

Add `AccountAuthorizationSummary` and `authorization_summary` to `Account`.

- [ ] **Step 2: Render authorization status**

In the account list “底层连接” column, show primary status, standby count, and a warning when `has_standby=false`. The warning must say the account can keep using the current session but needs a standby authorization before seamless switching can be claimed.

- [ ] **Step 3: Verify frontend typecheck**

Run: `cd frontend && npm run build`
Expected: PASS.

### Task 3: Integration Safety

**Files:**
- Review: `backend/app/services/developer_apps.py`
- Review: `backend/app/services/accounts.py`
- Review: `backend/app/services/messages.py`

- [ ] **Step 1: Keep current execution compatible**

Do not switch task dispatch to standby sessions in this phase. Existing code may continue reading account-level credentials while the new authorization asset table is introduced.

- [ ] **Step 2: Document explicit follow-up boundary in code comments only where needed**

Add a concise comment near the projection service explaining that fallback projection keeps current production accounts usable until explicit standby authorizations are created.

- [ ] **Step 3: Run focused regression tests**

Run: `cd backend && timeout 60 pytest tests/test_account_authorizations.py tests/test_account_availability.py tests/test_tenant_account_quota.py -q`
Expected: PASS.
