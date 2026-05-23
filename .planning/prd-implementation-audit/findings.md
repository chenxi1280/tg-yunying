# Findings

## Confirmed Findings

### F001 - ZIP package import is documented but not implemented

- Module: Material center.
- PRD/design evidence:
  - `docs/tg-ops-platform-prd.md:1357` promises `.zip` package upload for `.png`, `.jpg`, `.jpeg` imports.
  - `docs/tg-ops-platform-prd.md:1358` says ZIP is only an import container and invalid entries must be skipped with reasons.
  - `docs/tg-ops-platform-prd.md:1359` requires import result counts and per-file failure reasons.
- Implementation evidence:
  - `frontend/src/app/context/contentActions.ts:48` routes single file uploads to `/materials/upload`.
  - `frontend/src/app/context/contentActions.ts:56` routes multiple selected files to `/materials/upload/batch`.
  - `backend/app/config.py:158` defines allowed upload MIME types without ZIP by default.
  - `backend/app/services/material_ingestion.py:178` validates uploaded material files as direct material assets.
  - `backend/app/services/material_ingestion.py:187` rejects content types not in `material_allowed_upload_types`.
- Status: missing.
- Severity: P1.
- Suggested acceptance test: upload a ZIP containing valid JPG/PNG, hidden `__MACOSX`, a non-image file, an oversize image, and a duplicate; verify valid images become materials and the result report shows success, skipped, duplicate, oversize, and per-file reasons.

### F002 - Material import job model and async result page are missing

- Module: Material center.
- PRD/design evidence:
  - `docs/material-library-design.md:146` requires ZIP parsing, validation, de-duplication, and cache upload to enter an async import job with result counts.
  - `docs/material-library-design.md:430` requires `material_import_jobs` with source filename, import type, target group/pack, counts, status, summary, and per-file details.
  - `docs/tg-ops-platform-prd.md:2460` lists `POST /api/materials/upload/zip`.
  - `docs/tg-ops-platform-prd.md:2461` lists `GET /api/material-imports/{import_id}`.
  - `docs/tg-ops-platform-prd.md:2473` defines the async import and query behavior.
- Implementation evidence:
  - Search for `material_import_jobs`, `MaterialImport`, `material-imports`, and `upload/zip` only finds docs, not backend models, migrations, routers, services, or frontend calls.
  - `backend/app/api/routers/ai_config.py:212` and `backend/app/api/routers/ai_config.py:245` only expose direct upload and batch direct upload.
  - `backend/app/services/ai_config.py:750` creates one material row per direct uploaded file and does not create an import job.
- Status: missing.
- Severity: P1.
- Suggested acceptance test: submit ZIP import, receive `import_id`, poll import detail, and verify result counts plus per-file reasons persist after page refresh.

### F003 - Material center page is partial compared with required page operations

- Module: Material center.
- PRD/design evidence:
  - `docs/material-library-design.md:975` to `docs/material-library-design.md:987` requires overview, sticker library, avatar pack, media, upload/batch upload, TG cache status, tag/group management, usage records, and failure/fallback records.
  - `docs/material-library-design.md:990` to `docs/material-library-design.md:1003` requires preview status, file size, usage count, recent usage, recent failure reason, preview, edit, refresh cache, disable, and usage-record operations.
  - `docs/tg-ops-platform-prd.md:1661` to `docs/tg-ops-platform-prd.md:1665` requires sticker/avatar/image grouping, ZIP import, and import result traceability.
- Implementation evidence:
  - `frontend/src/app/views/MaterialsView.tsx:70` to `frontend/src/app/views/MaterialsView.tsx:144` renders title, type, review status, tags/group text, cache status, versions, reference impact, edit, disable, and restore.
  - `frontend/src/app/views/MaterialsView.tsx:201` to `frontend/src/app/views/MaterialsView.tsx:209` renders coarse tabs: all, stickers, avatars, media.
  - `frontend/src/app/views/MaterialsView.tsx:212` to `frontend/src/app/views/MaterialsView.tsx:222` shows reference boundary text but not a usage-record/detail flow.
  - `frontend/src/app/views/MaterialsView.tsx` has no per-material preview, per-material cache refresh, usage-record drawer, explicit group management, ZIP import, or import result page.
- Status: partial.
- Severity: P2.
- Suggested acceptance test: for an uploaded material, verify the row supports preview, cache refresh, usage record/detail, disable/restore, reference warning, and recent failure display; verify sticker/avatar/media groups can be managed without relying only on title/tag heuristics.

### F004 - Material center target APIs listed in PRD are absent or only partially represented

- Module: Material center API surface.
- PRD/design evidence:
  - `docs/tg-ops-platform-prd.md:2481` to `docs/tg-ops-platform-prd.md:2496` lists material detail, ZIP upload, import detail, versions, groups, references, refresh-cache, and cache config endpoints.
  - `docs/tg-ops-platform-prd.md:2502` requires `GET /api/materials/cache/config` and `PATCH /api/materials/cache/config`.
- Implementation evidence:
  - `backend/app/api/routers/ai_config.py:180` exposes `GET /api/materials`.
  - `backend/app/api/routers/ai_config.py:189` exposes `GET /api/materials/cache/health`, not cache config get/patch.
  - `backend/app/api/routers/ai_config.py:212`, `backend/app/api/routers/ai_config.py:245`, `backend/app/api/routers/ai_config.py:278`, `backend/app/api/routers/ai_config.py:296`, and `backend/app/api/routers/ai_config.py:314` expose upload, batch upload, patch material, disable, and restore.
  - No backend route was found for `GET /api/materials/{material_id}`, `POST /api/materials/upload/zip`, `GET /api/material-imports/{import_id}`, `POST /api/materials/{material_id}/versions`, `GET/POST/PATCH /api/material-groups`, `GET /api/materials/{material_id}/references`, `POST /api/materials/{material_id}/refresh-cache`, or cache config get/patch.
- Status: partial.
- Severity: P1.
- Suggested acceptance test: compare the PRD material API list with OpenAPI/backend route output and require every documented endpoint to either exist or be explicitly marked as future/not implemented in the PRD.

### F005 - Source-filter override endpoint and audit semantics are missing

- Module: Task center / group relay source filtering.
- PRD/design evidence:
  - `docs/tg-ops-platform-prd.md:2418` lists `POST /api/tasks/{task_id}/source-filter-overrides`.
  - `docs/tg-ops-platform-prd.md:2449` says this endpoint must update only the current task override, carry stable sender identity, source action, reason, and actor, and not modify a published rule version.
  - `docs/group-relay-source-filter-upgrade-plan.md:107` to `docs/group-relay-source-filter-upgrade-plan.md:111` require recent sender selection, manual paste, and task-local blocklist behavior.
- Implementation evidence:
  - `frontend/src/app/views/TaskCenterView.tsx:707` to `frontend/src/app/views/TaskCenterView.tsx:721` adds a source identity to `excluded_sender_*` and calls `PATCH /tasks/{task_id}/settings`.
  - `backend/app/api/routers/task_center.py:205` exposes generic `PATCH /api/tasks/{task_id}/settings`.
  - No backend route was found for `POST /api/tasks/{task_id}/source-filter-overrides`.
  - Current quick blocklist path does not visibly require reason/source action, so it cannot satisfy the PRD audit semantics without further evidence.
- Status: partial.
- Severity: P1.
- Suggested acceptance test: from task detail, add a recent sender to the source blocklist; verify only current task config changes, an audit record includes source action, stable sender identity, reason, and actor, and no rule version is modified.

### F006 - Message sending PRD API surface is not aligned with implementation naming and detail/precheck endpoints

- Module: Message sending.
- PRD/design evidence:
  - `docs/tg-ops-platform-prd.md:2320` to `docs/tg-ops-platform-prd.md:2327` lists `GET /api/message-send-tasks`, `GET /api/message-send-tasks/{task_id}`, per-task precheck, dispatch, retry, and cancel under the same naming.
  - `docs/tg-ops-platform-prd.md:2306` says message sending must return send records, target resolution, account precheck result, failure reason, and operation issue rollup status.
- Implementation evidence:
  - `backend/app/api/routers/message_tasks.py:26` exposes list as `GET /api/message-tasks`, not `GET /api/message-send-tasks`.
  - `backend/app/api/routers/message_tasks.py:42` and `backend/app/api/routers/message_tasks.py:58` expose create under `/api/message-send-tasks` and `/api/message-send-tasks/batch`.
  - `backend/app/api/routers/message_tasks.py:71`, `backend/app/api/routers/message_tasks.py:85`, and `backend/app/api/routers/message_tasks.py:100` expose dispatch/retry/cancel under `/api/message-tasks/{task_id}/...`.
  - No backend route was found for `GET /api/message-send-tasks/{task_id}` or `POST /api/message-send-tasks/{task_id}/precheck`.
  - `frontend/src/app/views/MessageSendingView.tsx:270` to `frontend/src/app/views/MessageSendingView.tsx:287` uses `POST /risk-control/preflight` before creation rather than a message-send-task precheck endpoint.
- Status: partial.
- Severity: P2.
- Suggested acceptance test: document and implement one consistent message task API naming scheme; verify detail, precheck, create, dispatch, retry, and cancel all work and return the PRD-required precheck/failure/rollup fields.

### F007 - Listener and operation metrics API surfaces are partial

- Module: Listener center and operation metrics.
- PRD/design evidence:
  - `docs/tg-ops-platform-prd.md:2406` to `docs/tg-ops-platform-prd.md:2410` lists listener summary, switch, reset-watermark, events, and errors endpoints.
  - `docs/tg-ops-platform-prd.md:2419` to `docs/tg-ops-platform-prd.md:2421` lists operation metrics summary, reports, and export endpoints.
- Implementation evidence:
  - `backend/app/api/routers/operations_center.py:63` exposes listener summary.
  - `backend/app/api/routers/operations_center.py:68` exposes listener switch.
  - No backend route was found for listener reset-watermark, events, or errors.
  - `backend/app/api/routers/operations_center.py:95` exposes operation metrics summary.
  - `backend/app/api/routers/system.py:141` exposes `GET /api/reports`, but no backend route was found for `GET /api/operation-metrics/reports` or `POST /api/operation-metrics/export`.
- Status: partial.
- Severity: P2.
- Suggested acceptance test: from listener center, view recent events and errors and reset watermark with second confirmation; from operation data, request a report and trigger an audited export.

### F008 - Account batch actions still require pre-selected accounts instead of action-first selection

- Module: TG accounts / account security / profile initialization.
- PRD/design evidence:
  - `docs/tg-ops-platform-prd.md:595` to `docs/tg-ops-platform-prd.md:605` says account batch actions must use "click action first, then select accounts"; list selection is only a quick prefill.
  - `docs/account-security-hardening-design.md:501` to `docs/account-security-hardening-design.md:508` requires batch entries that do not depend on pre-selected rows.
- Implementation evidence:
  - Supervision audit found `frontend/src/app/views/AccountsView.tsx:329` to `frontend/src/app/views/AccountsView.tsx:332` disables profile init / 2FA / cleanup when no account is selected.
  - Supervision audit found `frontend/src/app/views/AccountSecurityBatchDrawer.tsx:105` to `frontend/src/app/views/AccountSecurityBatchDrawer.tsx:116` and related submit paths consume preselected accounts.
- Status: partial.
- Severity: P1.
- Suggested acceptance test: without selecting rows in account list, click profile initialization, set 2FA, and cleanup devices; each should open a drawer that supports filter/search/cross-page/range account selection and then precheck.

### F009 - Listener center lacks event/error/watermark handling loop

- Module: Listener center.
- PRD/design evidence:
  - `docs/tg-ops-platform-prd.md:146` requires listener account switching, watermark reset, and listener exception handling.
  - `docs/tg-ops-platform-prd.md:2406` to `docs/tg-ops-platform-prd.md:2410` lists summary, switch, reset-watermark, events, and errors endpoints.
  - `docs/tg-ops-platform.md:862` to `docs/tg-ops-platform.md:893` describes listener center operations and exception handling.
- Implementation evidence:
  - `backend/app/api/routers/operations_center.py:63` exposes listener summary.
  - `backend/app/api/routers/operations_center.py:68` exposes listener switch.
  - Supervision audit found `frontend/src/app/views/ListenerCenterView.tsx:92` to `frontend/src/app/views/ListenerCenterView.tsx:103` and `frontend/src/app/views/ListenerCenterView.tsx:204` to `frontend/src/app/views/ListenerCenterView.tsx:290` mainly cover summary/detail and switch.
  - No backend route was found for listener reset-watermark, event list, or error list.
- Status: partial.
- Severity: P1.
- Suggested acceptance test: create listener backlog/error fixtures; verify listener center can view events and errors, switch backup account, reset watermark with reason and second confirmation, and write audit.

### F010 - Operation target admission failure handling is incomplete

- Module: Operation targets / admission.
- PRD/design evidence:
  - `docs/tg-ops-platform-prd.md:136` requires target capability adjustment and admission retry to write reason and audit.
  - `docs/tg-ops-platform-prd.md:2036` to `docs/tg-ops-platform-prd.md:2052` defines target preparation and admission rules.
  - `docs/tg-ops-platform.md:478` to `docs/tg-ops-platform.md:494` describes admission failure handling.
- Implementation evidence:
  - `backend/app/api/routers/operations.py:56` to `backend/app/api/routers/operations.py:137` exposes target list/detail/sync/create/update/account-policy, but no explicit admission retry route.
  - Supervision audit found `frontend/src/app/views/OperationTargetsView.tsx:526` to `frontend/src/app/views/OperationTargetsView.tsx:545` mainly shows group account coverage, not an admission failure retry loop.
- Status: partial.
- Severity: P1.
- Suggested acceptance test: create a channel/group target with accounts that are not joined or cannot send; target detail should show failed accounts, reason, retry/readmission action, and audit record.

### F011 - Message-send task failures do not clearly roll up to operation issues

- Module: Message sending / operation center.
- PRD/design evidence:
  - `docs/tg-ops-platform-prd.md:143` requires send/cancel/retry audit.
  - `docs/tg-ops-platform-prd.md:2304` to `docs/tg-ops-platform-prd.md:2306` says message sending must return send records, target resolution, account precheck, failure reason, and operation issue rollup status.
  - `docs/tg-ops-platform-prd.md:2555` includes message sending precheck, send, records, cancel, retry, dispatch, and failure rollup in test scope.
- Implementation evidence:
  - `frontend/src/app/views/MessageSendingView.tsx:270` to `frontend/src/app/views/MessageSendingView.tsx:314` performs risk preflight and then creates send tasks.
  - Supervision audit found message-task failures are written as `MessageTaskAttempt` in message services, while `backend/app/services/runtime_summary.py:64` to `backend/app/services/runtime_summary.py:78` rolls task-center `Action` failures into operation issues.
  - No static evidence yet proves message task failures create `operation_issue`.
- Status: suspected.
- Severity: P1.
- Suggested acceptance test: force one message task dispatch failure and verify operation center/target workbench receives an operation issue with source task, target, failure type, readable reason, and retry/resolve path.

### F012 - Rules center version governance lacks full UI and audit semantics

- Module: Rules center.
- PRD/design evidence:
  - `docs/tg-ops-platform-prd.md:147` to `docs/tg-ops-platform-prd.md:148` requires publish/rollback/copy to write version diff and audit and forbids silent modification of published versions.
  - `docs/rules-center-design.md:235` to `docs/rules-center-design.md:270` defines rule version mechanism.
  - `docs/rules-center-design.md:690` to `docs/rules-center-design.md:725` defines version page behavior.
- Implementation evidence:
  - Supervision audit found `frontend/src/app/views/RulesCenterView.tsx:382` to `frontend/src/app/views/RulesCenterView.tsx:400` exposes publish/rollback without visible diff or reason capture.
  - Supervision audit found `frontend/src/app/views/RulesCenterView.tsx:482` to `frontend/src/app/views/RulesCenterView.tsx:499` lacks a visible copy action despite backend support.
  - `backend/app/api/routers/operations_center.py:250`, `backend/app/api/routers/operations_center.py:258`, and `backend/app/api/routers/operations_center.py:266` expose publish, copy, and rollback endpoints.
- Status: partial.
- Severity: P2.
- Suggested acceptance test: copy a published version, inspect diff, publish with reason, rollback with reason, verify audit contains version diff/reason and published versions are immutable.

### F013 - Operation issue handling UI uses weak reason capture and incomplete return-context behavior

- Module: Operation center / operation issues.
- PRD/design evidence:
  - `docs/tg-ops-platform-prd.md:242` to `docs/tg-ops-platform-prd.md:250` requires modal/drawer/deep-link handling with return context.
  - `docs/tg-ops-platform-prd.md:2178` to `docs/tg-ops-platform-prd.md:2199` requires each issue to identify target, related tasks, failure codes, affected accounts, and suggested action.
  - `docs/tg-ops-platform-prd.md:2536` to `docs/tg-ops-platform-prd.md:2537` includes operation issue processing in test scope.
- Implementation evidence:
  - Supervision audit found `frontend/src/app/views/OverviewView.tsx:292` to `frontend/src/app/views/OverviewView.tsx:320` has issue drawer/status actions but uses `window.prompt` for reasons.
  - `backend/app/services/runtime_summary.py:181` to `backend/app/services/runtime_summary.py:231` stores issue metadata and return context, but frontend restoration of filter/page/expanded row/scroll context was not found.
- Status: partial.
- Severity: P2.
- Suggested acceptance test: from operation center, process an issue by modal/drawer/deep-link; reason entry must be structured, and returning from target/task detail must restore original filter, page, expanded target, and source issue.

### F014 - Frontend permission gating is incomplete on multiple pages

- Module: Frontend permissions.
- PRD/design evidence:
  - `docs/tg-ops-platform-prd.md:124` to `docs/tg-ops-platform-prd.md:127` defines menu, button, backend write, and audit permissions.
  - `docs/tg-ops-platform-prd.md:2491` to `docs/tg-ops-platform-prd.md:2492` says every visible button needs an interface/local behavior and button permission coverage.
  - `docs/tg-ops-platform-prd.md:2544` includes frontend hidden/disabled and backend denial tests.
- Implementation evidence:
  - Supervision audit found operation plan/issue buttons in `OverviewView.tsx`, target create/edit/sync buttons in `OperationTargetsView.tsx`, message-send create/submit/cancel/retry/dispatch in `MessageSendingView.tsx`, and listener switch in `ListenerCenterView.tsx` need manual permission verification or are not visibly gated by the PRD permissions.
  - Backend middleware has many write permission rules, for example `backend/app/permission_middleware.py:94` to `backend/app/permission_middleware.py:109`, but frontend button state is not consistently passed like it is for `MaterialsView` and `TaskCenterView`.
- Status: partial.
- Severity: P1.
- Suggested acceptance test: create least-privilege users for each view-only permission and verify every create/edit/start/stop/export/danger button is hidden/disabled, and direct backend calls return 403 with audit.

### F015 - Permission naming diverges from PRD in AI, prompts, proxy, and message sending

- Module: Permission model.
- PRD/design evidence:
  - `docs/tg-ops-platform-prd.md:143` names `message_sending.manage`.
  - `docs/tg-ops-platform-prd.md:155` to `docs/tg-ops-platform-prd.md:157` names `ai.*`, `prompt_templates.*`, and `proxies.manage`.
- Implementation evidence:
  - `backend/app/auth.py:188` to `backend/app/auth.py:189` uses `message_sending.view` and `message_sending.create`.
  - `backend/app/auth.py:197` to `backend/app/auth.py:198` uses `risk.view` and `risk.manage`; `backend/app/auth.py:182` uses `accounts.proxy_bind`.
  - `backend/app/permission_middleware.py:82` to `backend/app/permission_middleware.py:88` protects AI providers and prompt templates with `system.secrets_manage` and `system.manage`, not the PRD-named permissions.
- Status: partial.
- Severity: P1.
- Suggested acceptance test: compare `ALL_PERMISSIONS` to the PRD matrix; either rename/alias permissions or update PRD and manual so product, UI, backend, and tests use one vocabulary.

### F016 - Some sensitive read APIs may not have explicit least-privilege rules

- Module: Backend permission middleware.
- PRD/design evidence:
  - `docs/tg-ops-platform-prd.md:126` says frontend hidden buttons cannot replace backend checks.
  - `docs/tg-ops-platform-prd.md:2492` requires backend write interface permission checks for dangerous and sensitive actions.
- Implementation evidence:
  - Supervision audit flagged routes such as account clone plans, verification tasks, channel comments, relay attribution report, and runtime config for low-permission direct-call verification.
  - `backend/app/permission_middleware.py` includes broad path rules for many endpoints, but the static audit has not yet proven every sensitive read has the intended minimum permission.
- Status: suspected.
- Severity: P2.
- Suggested acceptance test: with a low-permission logged-in user, directly request clone plans, verification tasks, channel comments, relay attribution report, runtime config, and admin/user routes; verify required 403s and audit for denials where appropriate.

### F017 - Material cache channel config accepts only env-level runtime values, not admin-friendly links

- Module: System settings / material runtime config.
- PRD/design evidence:
  - `docs/tg-ops-platform-prd.md:203` says normal admins should fill cache channel link, `@username`, or `t.me/c/...`; the system parses it into the peer required by runtime.
  - `docs/tg-ops-platform-prd.md:458` says the page must not require users to manually enter `-100...` peer ids, must save the raw user input, and must normalize it into an executable cache target.
  - `docs/tg-ops-platform-prd.md:464` to `docs/tg-ops-platform-prd.md:482` requires two independent inputs: material cache channel and source-media cache channel, support public links, `@username`, private `t.me/c/...` links, advanced peer id compatibility, normalization, health validation, `.env` fallback, and friendly health errors.
  - `docs/tg-ops-platform-prd.md:2495`, `docs/tg-ops-platform-prd.md:2496`, and `docs/tg-ops-platform-prd.md:2502` define `GET /api/materials/cache/config` and `PATCH /api/materials/cache/config`.
- Implementation evidence:
  - `frontend/src/app/views/SystemConfigView.tsx:180` to `frontend/src/app/views/SystemConfigView.tsx:206` renders the "提示词与素材运行配置" tab through `AISettingsView`, but passes only health data and no editable cache config form.
  - `frontend/src/app/views/AISettingsView.tsx:116` to `frontend/src/app/views/AISettingsView.tsx:145` displays cache health cards only.
  - `backend/app/config.py:150` and `backend/app/config.py:151` read `SOURCE_MEDIA_CACHE_PEER_ID` and `MATERIAL_CACHE_PEER_ID` directly from environment variables.
  - `backend/app/services/material_cache.py:16` to `backend/app/services/material_cache.py:19` uses `get_settings().material_cache_peer_id` directly and returns without work if it is empty.
  - `backend/app/api/routers/ai_config.py:189` exposes cache health, but no cache config get/patch route exists.
- Status: missing.
- Severity: P1.
- Suggested acceptance test: in system settings, enter `https://t.me/c/1234567890/55397` for material cache and `@example_cache` for source-media cache; save should store raw input, normalize to `-1001234567890` and `@example_cache` for runtime, prefer saved values over `.env`, perform a cache-account access check, write audit, and update material-center health without requiring restart.

## Candidate Findings

Pending broader inventory and sidecar auditor results.
