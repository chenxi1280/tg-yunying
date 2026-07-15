# Account Online Full-Pool Capacity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove hidden account-online processing caps, make Telegram probe concurrency explicit, and deploy the full-pool behavior with production evidence.

**Architecture:** The worker main thread keeps all SQLAlchemy reads and writes, builds immutable probe jobs, and delegates only Telegram network calls to a bounded thread pool. Drain page size and network concurrency are explicit deployment parameters; neither limits the number of accounts admitted to `desired_online`.

**Tech Stack:** Python 3, SQLAlchemy, pytest, Docker Compose, GitHub Actions, PostgreSQL, Telethon.

---

### Task 1: Preserve the caller's drain page size

**Files:**
- Modify: `backend/tests/test_account_online_probe_timing.py`
- Modify: `backend/app/services/account_online_state.py`

- [x] **Step 1: Rewrite the existing cap regression as the required behavior**

Rename `test_drain_account_online_keepalive_caps_large_probe_batches` to `test_drain_account_online_keepalive_uses_requested_probe_batch` and assert that `limit=500` returns 500 and calls `probe:500:commit_each=True`.

- [x] **Step 2: Run the focused test and verify RED**

Run the single test with the repository Python environment and a 60-second hard timeout. Expected: FAIL because the service returns 20 and calls the probe layer with 20.

- [x] **Step 3: Remove the internal cap**

Delete `ACCOUNT_ONLINE_PROBE_BATCH_LIMIT` and replace:

```python
batch_limit = min(max(1, limit), ACCOUNT_ONLINE_PROBE_BATCH_LIMIT)
```

with:

```python
batch_limit = max(1, limit)
```

- [x] **Step 4: Run the focused test and verify GREEN**

Expected: PASS with the probe layer receiving 500.

### Task 2: Make Telegram probe concurrency explicit and validated

**Files:**
- Modify: `backend/tests/test_config_safety.py`
- Modify: `backend/tests/test_account_online_state.py`
- Modify: `backend/app/config.py`
- Modify: `backend/app/services/account_online_probe.py`

- [x] **Step 1: Add failing configuration and concurrency tests**

Add a settings test asserting `Settings(account_online_probe_concurrency=0)` raises `ValueError` containing `ACCOUNT_ONLINE_PROBE_CONCURRENCY`. Extend the concurrency test to monkeypatch `account_online_probe.get_settings` with `account_online_probe_concurrency=3`, create four jobs, and assert exactly three probes can be active before release.

- [x] **Step 2: Run both focused tests and verify RED**

Expected: the settings constructor does not accept the new field and the probe implementation still uses the fixed constant 4.

- [x] **Step 3: Add the explicit setting and validation**

Add:

```python
account_online_probe_concurrency: int = int(os.getenv("ACCOUNT_ONLINE_PROBE_CONCURRENCY", "32"))
```

to `Settings`, and in `__post_init__` raise a clear `ValueError` when it is less than one.

- [x] **Step 4: Inject the setting at the network boundary**

Import `get_settings`, remove `ONLINE_PROBE_MAX_CONCURRENCY`, and calculate:

```python
worker_count = min(get_settings().account_online_probe_concurrency, len(jobs))
```

Keep `_run_health_probe` as the only thread-pool function so no ORM object or Session crosses the thread boundary.

- [x] **Step 5: Run both focused tests and verify GREEN**

Expected: both pass and configured concurrency is honored.

### Task 3: Publish production capacity defaults

**Files:**
- Modify: `backend/tests/test_worker_roles.py`
- Modify: `docker-compose.server.yml`
- Modify: `.env.production.example`
- Modify: `docs/04-ops/deployment/PRODUCTION_RUNTIME.md`

- [x] **Step 1: Add a failing compose contract test**

In the online-worker compose test, assert the shared environment includes `ACCOUNT_ONLINE_PROBE_CONCURRENCY` with default 32 and the account-online command uses `ACCOUNT_ONLINE_WORKER_DRAIN_LIMIT:-1000`.

- [x] **Step 2: Run the compose test and verify RED**

Expected: FAIL because concurrency is absent and drain default remains 500.

- [x] **Step 3: Update compose and production documentation**

Add `ACCOUNT_ONLINE_PROBE_CONCURRENCY: ${ACCOUNT_ONLINE_PROBE_CONCURRENCY:-32}` to `x-backend-env`, change the online worker drain default to 1000, and document that these are throughput controls rather than account admission limits.

- [x] **Step 4: Run the compose test and verify GREEN**

Expected: PASS.

### Task 4: Regression verification and implementation commit

**Files:**
- Verify all files changed in Tasks 1-3.

- [x] **Step 1: Run focused online-state, configuration, and worker-role tests**

Use the PostgreSQL test database if available; otherwise run all `no_postgres` focused contracts locally and require the full set in GitHub Actions. Every backend test command has a 60-second hard timeout.

- [x] **Step 2: Run static verification**

Run Python compilation, `git diff --check`, inspect the complete diff, and verify no hidden `ACCOUNT_ONLINE_PROBE_BATCH_LIMIT` or fixed concurrency remains.

- [x] **Step 3: Commit the implementation**

Commit only the scoped code, tests, compose, plan, and production runtime documentation.

### Task 5: Standard release and production E4 verification

**Files:**
- Release through existing GitHub Actions and production runtime; do not edit production source files.

- [x] **Step 1: Integrate through the repository release path**

Bring the verified implementation to `master`, then update `release` from `master` and push `release` to trigger `Deploy Production`.

- [x] **Step 2: Verify GitHub Actions and deployed revision**

Require the deploy workflow to succeed and the production release directory/image revision to match the target commit.

- [x] **Step 3: Verify service health and configuration**

Check the account-online container health, worker heartbeat age, drain errors, effective `ACCOUNT_ONLINE_PROBE_CONCURRENCY=32`, and command drain limit 1000.

- [ ] **Step 4: Verify full-pool online behavior**

Query all `desired_online=true` states. Require no missing state rows; report online, stale, blocked, login_required, last-probe freshness, and original failure strings. All due and probe-eligible accounts must complete a real probe within the 15-minute active window; real login-required accounts remain separately counted.

- [ ] **Step 5: Verify group coverage and comment tasks**

Compare all four group daily-coverage counts before and after the observation window and inspect current comment-task worker/actions. Require real progress or report the exact remaining external/account blocker without claiming recovery.

### Task 6: Remove production batch-level head-of-line blocking

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/app/telethon_lifecycle.py`
- Modify: `backend/app/integrations/telegram/gateway.py`
- Modify: `backend/app/services/account_online_probe.py`
- Modify: `backend/tests/test_account_online_probe_timing.py`
- Modify: `backend/tests/test_config_safety.py`
- Modify: `backend/tests/test_telethon_lifecycle.py`
- Modify: `backend/tests/test_worker_roles.py`
- Modify: `docker-compose.server.yml`
- Modify: `.env.production.example`
- Modify: product, feature, dataflow, design, and production runtime documents.

- [x] **Step 1: Capture failed E4 evidence**

The first production release had 669 desired accounts but only 40 probed in 15 minutes, 633 stale, and 582 still due. Logs repeatedly reported `Security error while unpacking a received message: Server replied with a wrong session ID` while `_run_health_probes` waited for its complete result list.

- [x] **Step 2: Add and verify failing tests**

Add tests proving a fast result is not yielded while a slow result remains pending, a dedicated probe timeout cannot be configured, the lifecycle cannot accept an operation-specific timeout, and production config lacks the timeout. Verify all tests fail for the expected missing behavior.

- [x] **Step 3: Stream completed results and bound health probes**

Use `as_completed` so the main thread applies and commits each completed immutable result immediately. Add validated `ACCOUNT_ONLINE_PROBE_TIMEOUT_SECONDS=30` and pass it only through `check_account_health` to `TelethonClientLifecycle.run`; keep the normal Telegram operation timeout unchanged.

- [x] **Step 4: Verify the second fix locally**

Run the five red/green tests, then the 66-test online-state, config, worker, and Telethon lifecycle regression, Python compilation, and `git diff --check`.

- [ ] **Step 5: Redeploy and repeat E4**

Commit, push `master`, merge into `release`, require a successful Deploy Production run, and repeat the 15-minute full-pool, four-group coverage, and comment-task checks.
