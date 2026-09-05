import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import AccountPacingReservation, Action, AuditLog, ExecutionAttempt, GenerationJob, Task
from app.services._common import _now
from app.services.task_center import service
from app.services.task_center.engagement_direct_cutover import (
    CutoverOperation, activate_cutover, preview_cutover, retire_cutover, verify_retirement,
)
from app.services.task_center.engagement_retirement_cleanup import cleanup_cutover_batch, require_cutover_cleanup
from app.services.task_center.ai_generation_job_finish import finish_owned_job
from app.services.task_center.engagement_replacement_config import replacement_payload
from tests.test_engagement_account_binding import _payload, _seed, _session


pytestmark = pytest.mark.no_postgres
SHA = "a" * 40
OPERATION = CutoverOperation("codex", "user-direct-cutover", SHA)


def _scope(session, *, count=1):
    _seed(session)
    tasks = [service.create_channel_like_task(session, 1,
        _payload(name=f"旧任务{index}", engagement_contract_version="legacy_v0"), "test") for index in range(count)]
    for task in tasks:
        task.status = "running"
        task.stats = {"total": 100, "success": 40}
    paused = service.create_channel_like_task(session, 1,
        _payload(name="保持暂停", engagement_contract_version="legacy_v0"), "test")
    paused.status = "paused"
    session.commit()
    spec = {"tenant_id": 1, "deployed_sha": SHA, "replacements": {task.id: {
        "engagement_contract_version": "unified_engagement_v1", "account_group_ids": [1, 2]}
        for task in tasks}}
    return tasks, paused, spec


def _build(session, old, payload):
    return service._new_task(session, old.tenant_id, old.type, payload)


def _retire(session, spec, **options):
    preview = json.loads(json.dumps(preview_cutover(session, spec), default=str))
    receipt = retire_cutover(session, preview, OPERATION, create_replacement=options.get("builder", _build))
    session.commit()
    return preview, receipt


def test_cutover_retains_old_history_and_repeated_apply_reuses_exact_replacements():
    with _session() as session:
        tasks, paused, spec = _scope(session)
        old = tasks[0]
        preview, receipt = _retire(session, spec)
        assert old.status == "stopped" and old.retired_at is not None
        assert old.stats == {"total": 100, "success": 40}
        new = session.get(Task, receipt["mapping"][old.id])
        assert new.id != old.id and new.status == "draft"
        assert new.stats["success_count"] == 0
        assert paused.status == "paused" and paused.retired_at is None
        assert retire_cutover(session, preview, OPERATION, create_replacement=_build) == receipt
        assert session.scalar(select(func.count(Task.id))) == 3
        verify_retirement(session, receipt)
        outcome = activate_cutover(session, receipt, OPERATION,
            start_replacement=service.start_task_in_transaction, require_cleanup=require_cutover_cleanup)
        assert outcome["activated"] == 1
        assert new.status == "running"
        session.commit()
        verify_retirement(session, receipt)


@pytest.mark.parametrize("drift", ["config", "new_running", "release"])
def test_preview_drift_prevents_retirement_or_new_task_creation(drift):
    with _session() as session:
        tasks, paused, spec = _scope(session)
        preview = preview_cutover(session, spec)
        operation = OPERATION
        if drift == "config":
            tasks[0].priority = 5
        elif drift == "new_running":
            paused.status = "running"
        else:
            operation = CutoverOperation("codex", "user-direct-cutover", "b" * 40)
        session.commit()
        with pytest.raises(ValueError, match="engagement_cutover_.*(conflict|changed)"):
            retire_cutover(session, preview, operation, create_replacement=_build)
        assert tasks[0].retired_at is None
        assert session.scalar(select(func.count(Task.id))) == 2


def test_failed_second_creation_rolls_back_first_draft_and_all_retirements():
    with _session() as session:
        tasks, _, spec = _scope(session, count=2)
        preview = preview_cutover(session, spec)
        count = 0

        def failing_builder(session, old, payload):
            nonlocal count
            count += 1
            if count == 2:
                raise ValueError("second_creation_rejected")
            return _build(session, old, payload)

        with pytest.raises(ValueError, match="second_creation_rejected"):
            retire_cutover(session, preview, OPERATION, create_replacement=failing_builder)
        session.rollback()
        assert session.scalar(select(func.count(Task.id))) == 3
        assert all(task.status == "running" and task.retired_at is None for task in tasks)
        assert session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "统一引擎旧任务退役")) == 0


def _work(session, old):
    actions = [Action(tenant_id=1, task_id=old.id, task_type=old.type, action_type="like_message",
        account_id=11, status=state) for state in ("pending", "executing", "unknown_after_send", "success")]
    session.add_all(actions)
    session.flush()
    attempts = [ExecutionAttempt(tenant_id=1, action_id=actions[index].id, account_id=11,
        status=state, gateway_call_started_at=_now(), result_snapshot={"original": state})
        for index, state in ((1, "gateway_call_started"), (2, "result_unknown"), (3, "success"))]
    session.add_all(attempts)
    session.flush()
    for index, action_id in enumerate((None, actions[0].id, actions[2].id)):
        session.add(AccountPacingReservation(tenant_id=1, task_id=old.id, account_id=11,
            pacing_slot_key=f"old:{index}", policy_version="original", due_at=_now(),
            release_not_before_at=_now(), effective_claim_at=_now(), action_id=action_id,
            state="bound" if action_id else "reserved"))
    jobs = [GenerationJob(tenant_id=1, task_id=old.id, obligation_type="old-test", obligation_id=f"job-{index}",
        generation_sequence=1, context_snapshot_version=1, state=state,
        generation_owner_id="previous-worker", evaluator_evidence={"original": state})
        for index, state in enumerate(("pending", "unknown"))]
    session.add_all(jobs)
    session.commit()
    return actions, attempts, jobs


def test_cleanup_abandons_only_unissued_work_and_preserves_called_unknown_and_success():
    with _session() as session:
        tasks, _, spec = _scope(session)
        actions, attempts, jobs = _work(session, tasks[0])
        original = [(item.status, item.gateway_call_started_at, item.result_snapshot) for item in attempts]
        _, receipt = _retire(session, spec)
        with pytest.raises(ValueError, match="cleanup_incomplete"):
            require_cutover_cleanup(session, receipt)
        result = cleanup_cutover_batch(session, receipt, OPERATION)
        session.commit()
        assert result["actions"] == 1 and result["jobs"] == 2
        assert not any(result["remaining"].values())
        assert [item.status for item in actions] == ["skipped", "executing", "unknown_after_send", "success"]
        assert original == [(item.status, item.gateway_call_started_at, item.result_snapshot) for item in attempts]
        assert jobs[0].state == "cancelled" and jobs[1].state == "unknown"
        assert all(not item.generation_owner_id and item.generation_lease_epoch == 1 for item in jobs)
        reservations = list(session.scalars(select(AccountPacingReservation).order_by(AccountPacingReservation.pacing_slot_key)))
        assert [item.state for item in reservations] == ["released", "released", "bound"]
        assert cleanup_cutover_batch(session, receipt, OPERATION)["actions"] == 0
        require_cutover_cleanup(session, receipt)


def test_replacement_config_drift_blocks_activation_without_resuming_old_task():
    with _session() as session:
        tasks, _, spec = _scope(session)
        _, receipt = _retire(session, spec)
        new = session.get(Task, receipt["mapping"][tasks[0].id])
        new.type_config = {**new.type_config, "target_likes_per_message": 123}
        session.commit()
        with pytest.raises(ValueError, match="replacement_readback_mismatch"):
            activate_cutover(session, receipt, OPERATION,
                start_replacement=service.start_task_in_transaction, require_cleanup=require_cutover_cleanup)
        assert new.status == "draft" and tasks[0].status == "stopped"


@pytest.mark.parametrize("jitter", [0, 0.2])
def test_fresh_unified_like_keeps_explicit_source_quantity_jitter(jitter):
    with _session() as session:
        tasks, _, spec = _scope(session)
        spec["replacements"][tasks[0].id]["like_count_jitter"] = jitter
        _, receipt = _retire(session, spec)
        new = session.get(Task, receipt["mapping"][tasks[0].id])
        assert new.type_config["like_count_jitter"] == jitter


def test_replacement_cannot_narrow_original_all_account_scope():
    with _session() as session:
        tasks, _, spec = _scope(session)
        spec["replacements"][tasks[0].id]["account_group_ids"] = [1]
        with pytest.raises(ValueError, match="account_scope_changed"):
            preview_cutover(session, spec)


def test_replacement_retains_original_internal_multi_day_pacing():
    with _session() as session:
        tasks, _, spec = _scope(session)
        tasks[0].pacing_config = {**tasks[0].pacing_config, "rolling_window_days": 3, "multi_day_rampup": True}
        session.commit()
        _, receipt = _retire(session, spec)
        new = session.get(Task, receipt["mapping"][tasks[0].id])
        assert new.pacing_config["rolling_window_days"] == 3
        assert new.pacing_config["multi_day_rampup"] is True


def test_group_replacement_requires_explicit_topic_rate_and_rebuilds_derived_pacing():
    with _session() as session:
        _seed(session)
        old = Task(tenant_id=1, type="group_ai_chat", name="旧活群", status="running",
            type_config={"target_group_id": 7, "daily_message_target": 2000},
            pacing_config={"daily_message_target": 2000, "fulfillment_soft_pacing_version": "old"})
        session.add(old)
        session.flush()
        overrides = {"engagement_contract_version": "unified_engagement_v1", "account_group_ids": [1, 2]}
        with pytest.raises(ValueError, match="topic_participation_rate"):
            replacement_payload(old, overrides)
        payload = replacement_payload(old, {**overrides, "topic_participation_rate": 0.3})
        assert payload.topic_participation_rate == 0.3 and payload.daily_message_target == 2000
        assert "daily_message_target" not in payload.pacing_config.model_dump()


def test_late_generation_result_cannot_become_ready_after_retirement():
    with _session() as session:
        tasks, _, spec = _scope(session)
        actions, _, jobs = _work(session, tasks[0])
        _retire(session, spec)
        claim = SimpleNamespace(job_id=jobs[0].id, action_id=actions[0].id,
            owner="previous-worker", job_version=1, generation_lease_epoch=0)
        with pytest.raises(RuntimeError, match="generation_result_task_retired"):
            finish_owned_job(session, claim, job=jobs[0], action=actions[0], state="ready")
        assert jobs[0].state == "pending"
