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

- [x] **Step 4: Verify full-pool online behavior**

Query all `desired_online=true` states. Require no missing state rows; report online, stale, blocked, login_required, last-probe freshness, and original failure strings. All due and probe-eligible accounts must complete a real probe within the 15-minute active window; real login-required accounts remain separately counted.

- [x] **Step 5: Verify group coverage and comment tasks**

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

- [x] **Step 5: Redeploy and repeat E4**

Commit, push `master`, merge into `release`, require a successful Deploy Production run, and repeat the 15-minute full-pool, four-group coverage, and comment-task checks.

### Task 7: Repair Dispatcher claim expiry and comment starvation

**Files:**
- Modify: `backend/app/services/task_center/service.py`
- Modify: `backend/app/services/task_center/dispatcher.py`
- Modify: `backend/tests/test_task_center_role_drains.py`
- Modify: `backend/tests/test_task_center_capacity_dispatch.py`
- Modify: product, dataflow, design, and plan documents.

- [x] **Step 1: Capture production evidence and product contract**

Record that four workers request 100 actions each while effective concurrency is 13, comments share priority 3 with about one thousand due likes and nearly two hundred views, and due comments repeatedly return to pending with `claim_expired`. Specify effective-concurrency claim sizing and post-comment anti-starvation ordering.

- [x] **Step 2: Add focused failing tests**

Prove a requested drain of 100 passes only effective concurrency to `claim_actions`, and prove due `channel_comment` membership / `post_comment` actions are selected before an older ordinary batch action at the same Task priority while hard-hourly sends remain first.

- [x] **Step 3: Implement the minimal dispatcher fix**

Calculate effective concurrency before claiming and use it as the claim limit. Add an explicit channel-comment task rank after hard-hourly rank and Task priority in both claim and due-action ordering.

- [x] **Step 4: Run Dispatcher regressions and static checks**

Run the focused red/green tests, Dispatcher role and capacity suites with a 60-second timeout, Python compilation, `git diff --check`, and full diff review.

- [x] **Step 5: Release and verify production E4**

Push `master`, merge into `release`, require Deploy Production success, then prove due comments stop cycling through `claim_expired` and reach real `success` with `telegram_msg_id` or expose a different genuine Telegram/account failure.

### Task 8: Stop account-online from retaining duplicate session connections

**Files:**
- Modify: `backend/app/integrations/telegram/gateway.py`
- Modify: `backend/tests/test_telethon_lifecycle.py`
- Modify: product, runtime, dataflow, design, and plan documents.

- [x] **Step 1: Capture production evidence**

Confirm 425 desired accounts fail with `TimeoutError`, account-online logs continuously report `Server replied with a wrong session ID`, desired accounts contain zero duplicate session ciphertext values, and the account-online container holds about 167 established TCP connections while each Dispatcher holds far fewer.

- [x] **Step 2: Add the failing lifecycle test**

Require account health to create a fresh client, avoid `_get_or_create_client`, execute the authorization probe, and disconnect in all terminal paths.

- [x] **Step 3: Implement ephemeral health clients**

Use `_new_client`, a bounded connect, and `finally: disconnect()` inside `_health_async`; run the 30-second probe timeout inside the event loop, wait for a bounded 5-second cleanup before the synchronous caller returns, preserve the original probe exception when cleanup also fails, and keep the business client cache unchanged.

- [x] **Step 4: Run regressions and release**

Run focused lifecycle, online-state, timing, config, worker, and Dispatcher tests with 60-second limits, compile and diff checks, then publish through `master -> release -> Deploy Production`.

- [x] **Step 5: Verify production E4**

Require the new revision and healthy worker, account-online established TCP connections to remain bounded after a full probe cycle, `wrong session ID` / TimeoutError volume to decline, and online / group coverage to improve without reintroducing an account admission cap.

E4 evidence: release `3c3bd889` and workflow `29417207535` succeeded; account-online completed all 582 probe-eligible desired accounts, producing 534 online and 48 blocked while 87 login-required accounts remained separately classified. Established TCP connections stayed near configured concurrency during the batch and fell from the old baseline of about 167 to 2 after completion; new logs contained zero `wrong session ID` and zero `TimeoutError`. Due channel-comment actions were zero, and group coverage resumed growth while blocker counts fell sharply.

### Task 9: Eliminate overlapping AI generation batches inside one Dispatcher claim

**Files:**
- Modify: `backend/app/services/task_center/service.py`
- Modify: `backend/tests/test_task_center_role_drains.py`
- Modify: AI generation transaction design, PRD, dataflow index, production runtime, and this plan.

- [x] **Step 1: Capture the post-release production failure**

After `3c3bd889`, all workers were healthy but host load remained about 26. PostgreSQL continuously reported two Dispatcher processes deadlocking while both executed `UPDATE actions SET payload=?, result=? WHERE actions.id=?`. Today had thousands of `hard_hourly_bucket_expired` skips and hundreds of `send_message action 缺少可发送文案` / execution timeout failures, while confirmed coverage remained far below the daily denominator.

- [x] **Step 2: Trace the overlapping transaction source**

One worker claims up to its effective concurrency with one claim token and then submits every claimed Action to its thread pool. Each pending normal AI Action independently loads near-term siblings with that same token, commits before provider I/O, and later updates the overlapping batch, so sibling threads acquire the same Action rows in different orders.

- [x] **Step 3: Add a failing concurrency regression**

Create a production-dialect role-drain test with two shared-token pending AI Actions and prove both are currently dispatched concurrently. Require maximum active dispatch count to be one for that claim batch while ordinary non-shared Actions retain existing parallel error isolation behavior.

- [x] **Step 4: Implement the minimal claim-batch serialization**

Detect blank normal `send_message` Actions with pending generation, a generation id, and the snapshotted claim token. Serialize only that worker claim batch; do not change claim size, action scope, account scope, other task types, or cross-worker concurrency.

- [ ] **Step 5: Verify, release, and collect E4**

Run focused role-drain and AI generation regressions, compile/diff checks, publish through `master -> release -> Deploy Production`, then require no new post-release Action deadlocks, healthy workers, lower load/lock pressure, and increasing real coverage confirmations. Keep login-required, paused tasks, invalid targets, and remaining Telegram/account failures separately visible.

### Task 10: Remove per-slot tenant memory rescans in AI generation Phase C

**Files:**
- Modify: `backend/app/services/task_center/ai_message_memory.py`
- Modify: `backend/app/services/task_center/ai_generation_persistence.py`
- Modify: `backend/app/services/task_center/ai_generation_quality.py`
- Modify: `backend/tests/test_ai_group_message_memory_query_shape.py`
- Modify: product, dataflow, AI generation transaction design, production runtime, and this plan.

- [x] **Step 1: Capture the post-deadlock production bottleneck**

PostgreSQL showed repeated tenant-wide `ai_group_message_memory` similarity reads while Dispatcher Actions stayed in `provider_call_started / generation_claimed`. The live 7-day window contained about 9,877 dedup rows and one projected scan took about 2.76 seconds; the old Phase C repeated the 1-hour and 7-day scans for every accepted slot and eagerly executed later checks even after an exact duplicate was already found.

- [x] **Step 2: Add failing query-shape regressions**

Prove three accepted slots in one generation batch currently issue more than one semantic-window projection, and prove a 5-minute exact duplicate still executes semantic-window scans before returning.

- [x] **Step 3: Cache the generation-level tenant window and short-circuit checks**

Create one `DuplicateMemoryBatch` per persisted generation result batch. Load the tenant-level 7-day lightweight projection once, derive the 1-hour rows from that snapshot, append each newly reserved slot, and use an indexed `updated_at` overlap query before later slots to merge other Dispatcher commits. Evaluate exact, 1-hour, 7-day, and template checks sequentially so a confirmed earlier match stops later work. Do not narrow dedup to one group, use a stale snapshot, or omit pending/reserved/executing/unknown/success states.

- [x] **Step 4: Run focused and broader regressions**

Require the red/green query tests, all no-PostgreSQL AI memory/generation tests, task-center role drains, capacity dispatch regressions, Python compilation, and `git diff --check` to pass within their 60-second test limits.

- [ ] **Step 5: Release and collect production E4**

Publish through `master -> release -> Deploy Production`. After the new workers start, require healthy services, no Action deadlocks, reduced repeated semantic-window reads/Phase C duration, real coverage confirmations continuing to increase, and current comment-task status. Report login-required and genuine Telegram/account failures separately; do not claim all 580 obligations complete without ledger evidence.

### Task 11: Eliminate CPU-bound full similarity matching inside the cached tenant window

- [x] **Step 1: Capture production evidence**

After Task 10 release, all workers stayed healthy and the 7-day window was loaded once per generation, but shared generation Actions remained `executing` for more than 15 minutes. All four Dispatcher processes showed running Python threads rather than socket waits, PostgreSQL remained CPU-active, and Phase C sessions stayed open while each candidate still invoked `SequenceMatcher` across roughly ten thousand cached rows.

- [x] **Step 2: Add failing equivalence and fast-reject tests**

Require a threshold predicate to match the existing `max(SequenceMatcher ratio, char Jaccard)` decision and prove low-overlap text does not invoke `SequenceMatcher`.

- [x] **Step 3: Implement strict upper-bound pruning**

Cache bounded immutable character profiles. Return immediately on Jaccard acceptance; otherwise use character multiset overlap as a strict upper bound for the SequenceMatcher ratio and skip only rows that mathematically cannot reach the configured threshold. Preserve tenant-wide 1-hour / 7-day scope and thresholds.

- [ ] **Step 4: Run regressions, release, and collect production E4**

Run equivalence, memory, generation, Dispatcher and PostgreSQL CI coverage, publish through `master -> release`, then prove Phase C duration and executing backlog fall while the four current-day coverage ledgers continue increasing. Keep genuine offline/login-required accounts visible.

### Task 12: Give every account health probe its own event loop

**Files:**
- Modify: `backend/app/integrations/telegram/mock.py`
- Modify: `backend/app/integrations/telegram/gateway.py`
- Modify: `backend/app/services/account_online_probe.py`
- Modify: `backend/tests/test_telethon_lifecycle.py`
- Modify: `backend/tests/test_account_online_probe_timing.py`
- Modify: `docs/01-product/tg-ops-platform-prd.md`
- Modify: `docs/00-index/project-dataflow-index.md`
- Modify: `docs/00-index/project-structure-index.md`
- Modify: `docs/04-ops/deployment/PRODUCTION_RUNTIME.md`

- [x] **Step 1: Add a failing isolated-loop Gateway test**

Add a test that replaces `_run` with an assertion failure, calls `check_account_health_isolated`, and records the caller thread plus the thread used by `connect`, `is_user_authorized`, `get_me`, and `disconnect`:

```python
def test_account_health_isolated_runs_on_calling_thread(monkeypatch):
    gateway = TelethonTelegramGateway(Settings())
    caller_thread = threading.get_ident()
    observed_threads: list[int] = []

    class FakeClient:
        async def connect(self):
            observed_threads.append(threading.get_ident())

        async def is_user_authorized(self):
            observed_threads.append(threading.get_ident())
            return True

        async def get_me(self):
            observed_threads.append(threading.get_ident())

        async def disconnect(self):
            observed_threads.append(threading.get_ident())

    monkeypatch.setattr("app.integrations.telegram.gateway.decrypt_session", lambda _value: "raw-session")
    monkeypatch.setattr(gateway, "_new_client", lambda *_args, **_kwargs: FakeClient())
    monkeypatch.setattr(gateway, "_run", lambda *_args, **_kwargs: pytest.fail("isolated probe must not use process lifecycle"))
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)

    assert gateway.check_account_health_isolated("encrypted-session", credentials).status == "在线"
    assert observed_threads == [caller_thread] * 4
```

- [x] **Step 2: Add a failing account-online routing test**

Add a focused test proving the worker calls the isolated entry and never the process-wide entry:

```python
def test_health_probe_uses_isolated_gateway_entry(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.account_online_probe.gateway.check_account_health_isolated",
        lambda *_args: calls.append("isolated") or AccountHealth(status=AccountStatus.ACTIVE.value, health_score=95, detail="ok"),
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.account_online_probe.gateway.check_account_health",
        lambda *_args: pytest.fail("account-online must not use the process-wide loop"),
    )

    results = list(_run_health_probes([OnlineProbeJob(account_id=1, session_ciphertext="session", credentials=object())]))

    assert calls == ["isolated"]
    assert results[0].health.status == AccountStatus.ACTIVE.value
```

- [x] **Step 3: Run both tests and verify RED**

Run:

```bash
timeout 60 backend/.venv/bin/pytest -q \
  backend/tests/test_telethon_lifecycle.py::test_account_health_isolated_runs_on_calling_thread \
  backend/tests/test_account_online_probe_timing.py::test_health_probe_uses_isolated_gateway_entry
```

Expected: both fail because the isolated Gateway entry does not exist and `_run_health_probe` still calls `check_account_health`.

- [x] **Step 4: Implement the isolated health boundary**

In the mock Gateway, add an explicit alias so tests and non-production integration retain identical health semantics:

```python
def check_account_health_isolated(
    self,
    session_ciphertext: str | None,
    credentials: DeveloperAppCredentials | None = None,
) -> AccountHealth:
    return self.check_account_health(session_ciphertext, credentials)
```

In `TelethonTelegramGateway`, execute the already bounded ephemeral-client coroutine in the current probe thread instead of `TelethonClientLifecycle.run`. Use a private event loop with a `probe + disconnect + grace` outer deadline so cancellation that is slow to settle cannot occupy the probe thread indefinitely:

```python
def check_account_health_isolated(
    self,
    session_ciphertext: str | None,
    credentials: DeveloperAppCredentials | None = None,
) -> AccountHealth:
    probe_timeout = self.settings.account_online_probe_timeout_seconds
    hard_timeout = probe_timeout + ACCOUNT_HEALTH_DISCONNECT_TIMEOUT_SECONDS
    return self._run_isolated_health(
        self._bounded_health_async(
            session_ciphertext,
            self._usable_credentials(credentials),
            probe_timeout,
        ),
        hard_timeout,
        ACCOUNT_HEALTH_RUN_GRACE_SECONDS,
    )
```

`_run_isolated_health` creates and installs a new event loop in the current thread, schedules `loop.stop` at the hard deadline, and converts an unfinished `run_until_complete` into `TimeoutError`. Its `finally` path cancels remaining tasks, gives them only `cleanup_grace` to settle, suppresses destruction logging only for tasks still pending after that bounded cleanup, clears the thread event loop, and closes it. Add a regression where `get_me` delays cancellation for 150 ms while test constants reduce the full outer deadline to 30 ms; require the isolated call to return `TimeoutError` within 100 ms without unawaited-coroutine warnings.

Change `_run_health_probe` to call `gateway.check_account_health_isolated`. Do not modify `check_account_health`, the process-wide lifecycle, the client cache, database Session ownership, configured concurrency, or timeout values.

- [x] **Step 5: Run focused and broad regressions**

Run the two red/green tests, then:

```bash
timeout 60 backend/.venv/bin/pytest -q -m no_postgres \
  backend/tests/test_account_online_probe_timing.py \
  backend/tests/test_account_online_state.py \
  backend/tests/test_telethon_lifecycle.py \
  backend/tests/test_config_safety.py \
  backend/tests/test_worker_roles.py
backend/.venv/bin/python -m compileall -q backend/app backend/tests
git diff --check
```

Expected: all selected tests pass, compilation succeeds, and no whitespace errors remain.

- [ ] **Step 6: Update contracts and release through the standard path**

Document that account-online network probes own a thread-local event loop while normal Telegram business operations retain the process-wide lifecycle. Commit the scoped code, tests, PRD, dataflow, structure index, runtime guide, spec, and plan. Push `master`, merge `master` into `release`, push `release`, and require `Deploy Production` success.

- [ ] **Step 7: Collect production E4**

After the account-online worker restarts, require all of the following:

- current release symlink and worker image match the release commit;
- worker health and heartbeat remain fresh;
- all due probe-eligible accounts finish a real probe within 15 minutes;
- `account_health_probe_failed / TimeoutError` does not recur as a batch-wide failure;
- TCP connections stay near configured concurrency during probing and return to the idle baseline afterward;
- all four Beijing-current-date coverage ledgers exist, confirmed counts increase during the active window, and remaining obligations retain exact account/login/permission blockers;
- the channel-comment task has no overdue open actions and recent attempts show real Telegram outcomes.

### Task 13: Schedule the next probe from each account's completion time

- [x] **Step 1: Capture the production recurrence**

The first 582-account probe started after a three-minute source reconciliation and finished about ten minutes later. Every result still received the batch-start timestamp, so its five-minute `next_probe_at` was already overdue and the worker immediately entered another full-pool cycle.

- [x] **Step 2: Add a failing completion-clock regression**

Use a controlled clock where the probe completes eight minutes after selection and require `last_probe_at` plus `next_probe_at` to use the completion timestamp.

- [x] **Step 3: Preserve per-account completion and anchor the next cycle after batch completion**

When production does not inject a fixed test time, preserve each worker result's actual completion in `last_probe_at`, keep streamed per-result commits, and after the iterator is exhausted move every completed result's `next_probe_at` (plus successful stale deadline) no earlier than the last completion in that drain plus its configured interval. Preserve explicit `now=` determinism for unit tests and do not add an account-count cap or silent fallback.

- [ ] **Step 4: Run regressions, release, and collect production E4**

Run account-online timing/state/lifecycle/config/worker regressions, compile, and publish through `master -> release`. Production must show a full cycle followed by a real idle interval, reduced database pressure, no immediate full-pool re-entry, and progressing current-day group coverage.

### Task 14: Restore explicit daily-coverage fallback in the Dispatcher pipeline

- [x] **Step 1: Capture the real remaining blocker**

Production task stats showed `skip_reason=quality_gate` and `quality_rejection_counts` dominated by `duplicate_message`; the generic task `last_error` incorrectly looked like a Provider outage. M3, M2.5, and Grok results were being rejected while the approved tenant-controlled static fallback was absent from the refactored pipeline.

- [x] **Step 2: Add red/green pipeline and Phase C regressions**

Cover all-stage quality rejection, all-stage Provider unavailability, cached-result revalidation, tenant switch disabled, distinct batch fallback content, outbound/message-memory persistence, and end-to-end Telegram dispatch state transitions.

- [x] **Step 3: Restore the scoped explicit fallback**

Only non-reply slots bound to a current daily-coverage ledger can become `emoji_react`. Persist `quality_fallback=emoji_react`, `human_quality_decision=explicit_static_quality_fallback`, `generation_source/fallback_stage=static_safe_fallback`, and the original rejection reason. Keep content policy, tenant-wide message-memory dedupe, fixed slot mapping, coverage reservation, and Telegram send gates unchanged.

- [ ] **Step 4: Release and collect production E4**

Publish through `master -> release`, require workflow success, then prove real current-day `success/confirmed` growth for active-window groups, exact visible blockers for unavailable accounts, no unexpected duplicate-memory rejection of the explicit fallback, and no overdue comment actions.
