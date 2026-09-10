# Task execution repair Release Gate

Intake: task-execution-repair-20260910; L3; merge_owner=current task.
Worktree: /private/tmp/tgyunying-task-execution-repair-20260910; original base=1e9358f8.
locked_paths: direct_action_claims/direct_action_candidates, fulfillment_remote_facts/channel_confirmed_fact/fulfillment_fact_ledger and related tests; current PRD and gate. Shared indexes integrated with current master. Root untracked ai-group-integration-recheck document preserved.

Authorization: current conversation allows deleting whole old stalled Tasks, pausing task workers, disposing stale related records, resuming, and fixing remaining execution problems.

## Production maintenance completed

2026-09-10 18:05–18:16 BJT, runtime 1e9358f8, existing release lock. Saved/stopped/restored 13 original execution workers; all workers subsequently healthy. Two approved comment Tasks c89f4bd0-9cb2-4e4c-9305-d3bfed34ce1b and d80c0050-7030-4367-af44-6ff2fcc62b5d deleted through delete_task (auditable tombstones retained). 11,921 uncalled expired channel Actions safely settled in 477 hash/version guarded batches; remaining open candidates in approved snapshot=0. Physical fact deletion=0. Unknown/called/claimed/active generation evidence excluded; existing paused/stopped neighbor Tasks unchanged.
Evidence: /tmp/tg-task-maintenance-20260910/task-maintenance-20260910-final/{preview.json,receipts.json}, run.log. Audit approval_ref=codex:task-execution-repair-20260910:user-delete-stalled-and-resume. These manifests are completed, not replay authority.

## Design and review

R2 expired/live selection separated, no Gateway in expiration; original ownership/epoch/source deadline and pacing settlement retained. R3 original 3d987665 revalidated and included as c1e5af1d (deadline and comment shortfall). R5 positive projection uses exact typed ownership plus later successful same-epoch Attempt; historical unknown remains conservative for retry. Tenant/task projection lock and monotonic positive confirmation preserve late unknown evidence. No migrations introduced by these changes.

Tests: original positive-fact regression fails before fix; after fix SQLite positive/negative identities and PostgreSQL committed confirmation pass. Expired/live fairness and independent PG locks pass. Existing source deadline/comment expiry/dispatch/pacing tests pass; final immutable candidate test evidence recorded in release manifest.
Readonly production candidate query: expired20/live20 in 1.177s/0.308s; positive typed chain resolves Attempt 3ad50a6c-93dd-4477-a44b-b57f9a5e563a for Action 88b80efe-0e62-48c3-9c6e-12ce29353517. Gateway receipt string is not typed table UUID. This check made no writes.

## Release and acceptance

Stages R1 maintenance_complete; R2/R3/R5 design_complete, dev_complete, targeted QA pass. Candidate artifact, runtime verification and postrelease typed evidence pending. R4 AI/search external reasons remain separately blocked/unproven until fresh evidence; healthy runtime is not business fulfillment.
