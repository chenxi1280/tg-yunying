# 2026-06-28 Release Deploy 26ce763

## Scope

- Promote release commit `26ce763de4050a1e0f7dbf7d06c8c2397bb6ef12`.
- Preserve the project release path by pushing the same commit to `master` before `release`.
- Verify GitHub Actions `Deploy Production` and production health checks.

## Evidence

- Local backend compile: `backend/.venv/bin/python -m compileall -q backend/app` passed.
- Local backend no-postgres tests: `291 passed, 855 deselected`.
- Local frontend build: `npm run build` passed.
- GitHub Actions run: `28297095336`.
- Jobs passed: `checks`, `build-images`, `deploy`.
- Production release: `20260627175721_26ce763`.
- Production checks passed: backend container healthy, planner/dispatcher/listener/recovery/account-security/metrics workers healthy, planner smoke check passed, frontend static index present, public frontend HTTP 200, public `/api/health` HTTP 200.

## Status

- `release_gate`: passed.
- `production_health`: passed.
- `business_specific_recheck`: unproven for L3 duplicate-send runtime until targeted prod-diagnosis evidence is collected.
