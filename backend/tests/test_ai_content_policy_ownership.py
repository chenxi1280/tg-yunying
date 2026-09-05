from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models import (
    AiContentPolicyVersion,
    AiContentWindowPlan,
    AiContentWindowPlanSlot,
    ContextScopeRevision,
    TaskAiContentPolicyBinding,
)
from app.services.task_center.ai_content_job_binding import (
    AiContentJobBindingError,
    bind_comment_generation_contract,
    bind_group_generation_contracts,
)
from tests.test_ai_content_job_binding import _add_group_item, _engine, _payload, _seed


pytestmark = pytest.mark.no_postgres
V2_CONFIG = {"ai_content_route_v2_enabled": True}


def _successor(session, task):
    session.get(AiContentPolicyVersion, "policy-1").status = "retired"
    session.flush()
    policy = AiContentPolicyVersion(
        id="policy-2", tenant_id=task.tenant_id, version=2, status="active",
        route_rules={"allowed_routes": ["general"]},
        prompt_registry={"general": {"version": "general_v2"}},
        gate_config={"forbidden_claim_categories": ["unsupported_claim"]},
        example_set={"version": "examples-v2"}, policy_hash="q" * 64,
    )
    task.config_revision += 1
    session.add_all([policy, TaskAiContentPolicyBinding(
        tenant_id=task.tenant_id, task_id=task.id,
        task_lifecycle_epoch=task.task_lifecycle_epoch,
        task_config_revision=task.config_revision, policy_version_id=policy.id,
        allowed_routes=["general"], evidence_hash="n" * 64, approved_by="reviewer",
    )])
    session.flush()


def _bind(session, task, action, job, kind):
    if kind == "group":
        return bind_group_generation_contracts(
            session, task, [(action, _payload(job.id))],
            config=V2_CONFIG, jobs=(job,),
        )["_ai_content_contracts"][job.id]
    return bind_comment_generation_contract(
        session, task, action=action, job=job, config=V2_CONFIG,
        payload=SimpleNamespace(channel_target_id=7, message_content="今天聊聊天气"),
    )["_ai_content_contract"]


@pytest.mark.parametrize("kind", ["group", "comment"])
def test_started_preparation_keeps_original_policy_after_task_revision(kind):
    with Session(_engine()) as session:
        task, action, job = _seed(session, routes=["general"])
        first = _bind(session, task, action, job, kind)
        original_slot_id = job.window_slot_id
        _successor(session, task)

        rebound = _bind(session, task, action, job, kind)

        assert rebound == first
        assert job.window_slot_id == original_slot_id
        assert job.task_binding_hash == "b" * 64
        assert job.content_policy_hash == "p" * 64
        assert job.example_set_version == "examples-v1"
        assert job.evaluator_evidence["generation_contract"]["task_topic_revision"] == 3


def test_started_preparation_does_not_require_current_revision_binding():
    with Session(_engine()) as session:
        task, action, job = _seed(session, routes=["general"])
        first = _bind(session, task, action, job, "group")
        task.config_revision += 1

        assert _bind(session, task, action, job, "group") == first


def test_mixed_batch_uses_each_jobs_policy_and_batches_reads():
    engine = _engine()
    with Session(engine) as session:
        task, action, job = _seed(session, routes=["general"])
        _bind(session, task, action, job, "group")
        _successor(session, task)
        second_action, second_job = _add_group_item(session, 2)
        counts = {"binding": 0}

        def record(_conn, _cursor, statement, _params, _context, _many):
            if "from task_ai_content_policy_bindings" in statement.lower():
                counts["binding"] += 1

        event.listen(engine, "before_cursor_execute", record)
        try:
            contracts = bind_group_generation_contracts(
                session, task,
                [(action, _payload(job.id)), (second_action, _payload(second_job.id))],
                config=V2_CONFIG, jobs=(job, second_job),
            )["_ai_content_contracts"]
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert contracts[job.id]["content_policy_hash"] == "p" * 64
        assert contracts[second_job.id]["content_policy_hash"] == "q" * 64
        assert counts == {"binding": 1}
        plans = list(session.scalars(select(AiContentWindowPlan)))
        assert {(p.task_config_revision, p.content_policy_hash) for p in plans} == {
            (3, "p" * 64), (4, "q" * 64),
        }


def test_context_refresh_retains_preparation_policy_in_replacement_window():
    with Session(_engine()) as session:
        task, action, job = _seed(session, routes=["general"])
        _bind(session, task, action, job, "group")
        original_slot_id = job.window_slot_id
        _successor(session, task)
        session.add(ContextScopeRevision(
            tenant_id=1, scope_type="group", scope_id="7",
            context_scope_revision=5, context_snapshot_hash="d" * 64,
        ))
        session.flush()

        contract = _bind(session, task, action, job, "group")

        assert contract["content_policy_hash"] == "p" * 64
        assert job.window_slot_id != original_slot_id
        slot = session.get(AiContentWindowPlanSlot, job.window_slot_id)
        plan = session.get(AiContentWindowPlan, slot.plan_id)
        assert plan.task_config_revision == 3
        assert plan.content_policy_hash == "p" * 64


@pytest.mark.parametrize("batch_size", [2, 25])
def test_reclaim_batches_policy_and_window_reads(batch_size):
    engine = _engine()
    with Session(engine) as session:
        task, action, job = _seed(session, routes=["general"])
        items = [(action, job)] + [
            _add_group_item(session, index) for index in range(2, batch_size + 1)
        ]
        batch = [(action, _payload(job.id)) for action, job in items]
        jobs = tuple(job for _action, job in items)
        bind_group_generation_contracts(session, task, batch, config=V2_CONFIG, jobs=jobs)
        counts = {"policy": 0, "window": 0}

        def record(_conn, _cursor, statement, _params, _context, _many):
            normalized = statement.lower()
            if "from task_ai_content_policy_bindings" in normalized:
                counts["policy"] += 1
            if "from ai_content_window_plan_slots" in normalized:
                counts["window"] += 1

        event.listen(engine, "before_cursor_execute", record)
        try:
            bind_group_generation_contracts(session, task, batch, config=V2_CONFIG, jobs=jobs)
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert counts == {"policy": 1, "window": 1}


@pytest.mark.parametrize("corruption", ["tenant", "task", "epoch", "policy", "binding"])
def test_inconsistent_frozen_policy_is_rejected_without_overwrite(corruption):
    with Session(_engine()) as session:
        task, action, job = _seed(session, routes=["general"])
        _bind(session, task, action, job, "group")
        slot = session.get(AiContentWindowPlanSlot, job.window_slot_id)
        plan = session.get(AiContentWindowPlan, slot.plan_id)
        if corruption == "binding":
            job.task_binding_hash = "wrong"
        else:
            field, value = {
                "tenant": ("tenant_id", 99), "task": ("task_id", "other-task"),
                "epoch": ("task_lifecycle_epoch", 99),
                "policy": ("content_policy_hash", "wrong"),
            }[corruption]
            setattr(plan, field, value)
        original = (job.window_slot_id, job.task_binding_hash, job.content_policy_hash)

        with pytest.raises(AiContentJobBindingError, match="generation_policy"):
            _bind(session, task, action, job, "group")

        assert (job.window_slot_id, job.task_binding_hash, job.content_policy_hash) == original
