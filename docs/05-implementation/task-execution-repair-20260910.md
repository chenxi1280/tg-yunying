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

Stages R1 maintenance_complete; R2/R3/R5 design_complete, dev_complete, targeted QA pass. Candidate 1129407a released; runtime and cleanup verified; scoped business evidence below. R4 AI/search external reasons remain separately blocked/unproven until fresh evidence; healthy runtime is not business fulfillment.

## 18:36 BJT release result and scoped acceptance

- Frozen candidate: 1129407aaffa13942c9a24a91c0683f57e003d74, includes current 2df94a9d master hotpath changes. Source master/release both matched at dispatch. Evidence-only documentation after release is separate from runtime SHA.
- Immutable candidate QA: 83 + 17 + 7 = 107 passed, each batch <60s (27.29/7.02/16.92 seconds). Frontend npm ci/build passed. Initial prepare-v1 was rejected for incorrect test selector before images/deployment; corrected v2 is the actual verified artifact.
- Artifact: linux/amd64, 612858183 bytes; archive SHA256 c2d572f2f86f82f48b3e3b6978b9336e9d8c1cc53ca55b29884294c9dfafd91c. Three image identities verified in local archive load and server load. Complete manifest and logs: /tmp/tg-task-maintenance-20260910/release-v2/.
- Deployment 18:29:01–18:36:07 BJT, exit0, release_passed; current=/data/tgyunying/releases/20260910182905_1129407a. API/static/provider/activation checks passed; independent runtime.json verified backend and workers at candidate. 34 runtime configuration keys preserved, fingerprint 7b760f4d8d1e818ca80b912f6e5c84aedf62bd1780628844201dda54b656effd. OCR worker healthy.
- Cleanup passed: exact previous successful 1e9358f8 backend/OCR images removed; identical current frontend image preserved. Previous transfer archive already absent; new server transfer package removed after verification; local new archive retained. No global prune, force removal or other-project cleanup.
- R5 exact production projection repair: preview hash e7983c8746ada9d44af23ec1f30b911ad790a2ddf9377cfbabeb43edbdc49dd4; action88b80efe same tenant/account/epoch and typed receipt validated again. Added reaction_observed fact c8ad2e18-9aa8-4b8c-ae48-2d75850b89bc from existing Attempt3ad50a6c; FOP remote_reconcile_only → confirmed. Audit approval_ref=codex:task-execution-repair-20260910:positive-projection. Separate readback passed: all11 historical Attempts unchanged; two unknown facts retained; Telegram calls=0. This corrects a historical success projection, not a new reaction.
- R2 runtime flow exercised: since new backend start18:33:09, 195 like and2 view safely_not_executed facts automatically produced, alongside7 view_observed facts. Remaining expired candidate sample still20, so no claim that the dynamically changing whole queue is empty; the earlier approved 11,921-row maintenance snapshot had zero residual open candidates. Expired original-deadline work remains unsent.
- Task preservation readback: two approved Tasks deleted; original comment64f009db and clone46b91930 remain paused, clonece341c64 remains stopped. Whole-task lifecycle pause was not used; stopping/restoring original workers preserved epochs and original human state.

Product acceptance is scoped: R1 maintenance verified, R2 expiry progression verified, R3 code and targeted source/comment tests verified but new like fulfillment remains unproven, R5 local projection corrected with pre-existing typed confirmation. R4 and complete Task goals remain blocked/unproven. Account freeze/session expiry/cannot_send/not_in_group, post-send visibility, provider/content validation and search verification/transport reasons remain exposed. No quality, quantity, permission or unknown-replay boundary was bypassed.

Final typed acceptance snapshot 2026-09-10T18:39:13.146741+08:00: running view5 cumulative confirmed3599, today1591, post-runtime10; AI10 ledger895/due14046 with post-runtime1 typed message; search318/1000 with post-runtime0; like6 confirmed2 historical and post-runtime0. Scope has22 running Tasks after deletion. These quantities use distinct counters and cannot be summed as one fulfillment result. Source: /tmp/tg-task-maintenance-20260910/postrelease-acceptance-e4.jsonl.
