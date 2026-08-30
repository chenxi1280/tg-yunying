from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AdultSubjectAttestation,
    AiContentPolicyVersion,
    AiContentWindowPlanSlot,
    ContextScopeRevision,
    GenerationJob,
    Task,
    TaskAiContentPolicyBinding,
)
from app.services.task_center.ai_content_job_binding import (
    AiContentJobBindingError,
    bind_group_generation_contracts,
    enrich_group_generation_slots,
)


pytestmark = pytest.mark.no_postgres


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def test_group_v2_binds_policy_window_and_generation_slot() -> None:
    with Session(_engine()) as session:
        task, action, job = _seed(session, routes=["general"])
        payload = _payload(job.id)

        config = bind_group_generation_contracts(
            session,
            task,
            [(action, payload)],
            config={"ai_content_route_v2_enabled": True},
        )
        slots = enrich_group_generation_slots(
            config,
            [(action, payload)],
            [{"slot_id": "slot-1"}],
        )
        session.flush()
        window_slot = session.scalar(select(AiContentWindowPlanSlot))

        assert window_slot is not None
        assert window_slot.claimed_by_job_id == job.id
        assert job.window_slot_id == window_slot.id
        assert job.task_direction_snapshot_hash == "b" * 64
        assert slots[0]["context_route"] == "general"
        assert slots[0]["route_evidence_ids"] == ["f1"]
        assert slots[0]["negative_phrases"] == ["签到"]
        frozen = job.evaluator_evidence["generation_contract"]
        assert frozen["context_revision"] == 4
        assert frozen["context_mode"] == "history"
        assert frozen["allowed_facts"] == {"f1": "今天群里挺热闹"}
        assert frozen["forbidden_claims"] == ["price"]
        assert frozen["content_route"] == "general"
        assert frozen["prompt_version"] == "general_v1"


def test_group_v2_weak_adult_jargon_stays_in_general_route() -> None:
    with Session(_engine()) as session:
        task, action, job = _seed(
            session,
            routes=["general", "adult_service_sensory"],
        )

        config = bind_group_generation_contracts(
            session,
            task,
            [(action, _payload(job.id, history="甲: 老师今晚在吗"))],
            config={"ai_content_route_v2_enabled": True},
        )

        assert config["_ai_content_contracts"][job.id]["content_mode"] == "general"


def test_group_v2_single_adult_route_still_requires_current_evidence() -> None:
    with Session(_engine()) as session:
        task, action, job = _seed(
            session,
            routes=["adult_service_sensory"],
        )

        with pytest.raises(AiContentJobBindingError, match="context_route_unproven"):
            bind_group_generation_contracts(
                session,
                task,
                [(action, _payload(job.id, history="甲: 今天天气不错"))],
                config={"ai_content_route_v2_enabled": True},
            )


def test_group_v2_rejects_low_information_context_before_routing() -> None:
    with Session(_engine()) as session:
        task, action, job = _seed(session, routes=["general"])

        with pytest.raises(
            AiContentJobBindingError,
            match="context_route_evidence_missing",
        ):
            bind_group_generation_contracts(
                session,
                task,
                [(action, _payload(job.id, history="甲: Qz5\n乙: j"))],
                config={"ai_content_route_v2_enabled": True},
            )


def test_group_v2_uses_frozen_general_topic_when_context_is_silent() -> None:
    with Session(_engine()) as session:
        task, action, job = _seed(session, routes=["general"])
        payload = _payload(job.id, history="甲: j")
        payload.topic_direction = {"title": "夜宵吃点啥"}

        config = bind_group_generation_contracts(
            session,
            task,
            [(action, payload)],
            config={"ai_content_route_v2_enabled": True},
        )

        contract = config["_ai_content_contracts"][job.id]
        assert contract["content_mode"] == "general"
        assert contract["route_evidence_ids"] == ["f1"]
        assert job.evaluator_evidence["generation_contract"]["context_mode"] == "silence"


def test_group_v2_does_not_use_adult_topic_config_as_current_evidence() -> None:
    with Session(_engine()) as session:
        task, action, job = _seed(
            session,
            routes=["general", "adult_service_sensory"],
        )
        payload = _payload(job.id, history="甲: j")
        payload.topic_direction = {"title": "老师今晚水多不"}

        with pytest.raises(
            AiContentJobBindingError,
            match="context_route_evidence_missing",
        ):
            bind_group_generation_contracts(
                session,
                task,
                [(action, payload)],
                config={"ai_content_route_v2_enabled": True},
            )


def test_group_v2_routes_current_sensory_evidence_without_global_adult_mode() -> None:
    with Session(_engine()) as session:
        task, action, job = _seed(
            session,
            routes=["general", "adult_service_sensory"],
        )
        config = bind_group_generation_contracts(
            session,
            task,
            [(action, _payload(job.id, history="甲: 水滋滋，看着好润"))],
            config={"ai_content_route_v2_enabled": True},
        )

        assert config["_ai_content_contracts"][job.id]["content_mode"] == (
            "adult_service_sensory"
        )


def test_group_v2_does_not_treat_common_adjectives_as_adult_evidence() -> None:
    with Session(_engine()) as session:
        task, action, job = _seed(
            session,
            routes=["general", "adult_service_sensory"],
        )
        config = bind_group_generation_contracts(
            session,
            task,
            [(action, _payload(job.id, history="甲: 袋子挺紧，路上有点滑"))],
            config={"ai_content_route_v2_enabled": True},
        )

        assert config["_ai_content_contracts"][job.id]["content_mode"] == "general"


@pytest.mark.parametrize(
    "history",
    (
        "甲: 这电影真好看",
        "甲: 手机一直震动",
        "甲: 夜宵多少钱",
    ),
)
def test_group_v2_common_topics_do_not_trigger_adult_routes(history: str) -> None:
    with Session(_engine()) as session:
        task, action, job = _seed(
            session,
            routes=[
                "general",
                "adult_visual",
                "adult_product",
                "adult_service_inquiry",
                "adult_service_sensory",
            ],
        )
        config = bind_group_generation_contracts(
            session,
            task,
            [(action, _payload(job.id, history=history))],
            config={"ai_content_route_v2_enabled": True},
        )

        assert config["_ai_content_contracts"][job.id]["content_mode"] == "general"


def test_group_v2_alternates_inquiry_and_sensory_when_both_are_grounded() -> None:
    with Session(_engine()) as session:
        task, first_action, first_job = _seed(
            session,
            routes=["adult_service_inquiry", "adult_service_sensory"],
        )
        second_action, second_job = _add_group_item(session, 3)
        history = "甲: 老师今晚能约吗，看着好润"

        config = bind_group_generation_contracts(
            session,
            task,
            [
                (first_action, _payload(first_job.id, history=history)),
                (second_action, _payload(second_job.id, history=history)),
            ],
            config={"ai_content_route_v2_enabled": True},
        )

        modes = {
            item["content_mode"]
            for item in config["_ai_content_contracts"].values()
        }
        assert modes == {"adult_service_inquiry", "adult_service_sensory"}


def test_group_v2_selects_one_grounded_route_when_visual_and_inquiry_match() -> None:
    with Session(_engine()) as session:
        task, first_action, first_job = _seed(
            session,
            routes=[
                "adult_visual",
                "adult_service_inquiry",
                "adult_service_sensory",
            ],
        )
        binding = session.scalar(select(TaskAiContentPolicyBinding))
        visual_attestation = AdultSubjectAttestation(
            id="visual-attestation-1", tenant_id=1, scope_type="task_group",
            scope_id="7", subject_class="adult_visual",
            evidence_codes=["adult_visual_content_verified"],
            permission_snapshot={"adult_content_attest": True},
            expires_at=datetime(2027, 8, 19), task_config_revision=3,
            policy_version=1, status="active", evidence_hash="v" * 64,
        )
        session.add(visual_attestation)
        binding.attestation_ids = [*binding.attestation_ids, visual_attestation.id]
        second_action, second_job = _add_group_item(session, 3)
        history = "甲: 她丝袜挺好看，今晚能约吗"

        config = bind_group_generation_contracts(
            session,
            task,
            [
                (first_action, _payload(first_job.id, history=history)),
                (second_action, _payload(second_job.id, history=history)),
            ],
            config={"ai_content_route_v2_enabled": True},
        )

        modes = {
            item["content_mode"]
            for item in config["_ai_content_contracts"].values()
        }
        assert modes == {"adult_visual", "adult_service_inquiry"}


def test_group_v2_rebinds_pre_gateway_slot_to_current_context_revision() -> None:
    with Session(_engine()) as session:
        task, action, job = _seed(session, routes=["general"])
        first = bind_group_generation_contracts(
            session,
            task,
            [(action, _payload(job.id))],
            config={"ai_content_route_v2_enabled": True},
        )
        old_slot_id = job.window_slot_id
        session.add(ContextScopeRevision(
            tenant_id=1,
            scope_type="group",
            scope_id="7",
            context_scope_revision=5,
            context_snapshot_hash="d" * 64,
        ))
        session.flush()

        second = bind_group_generation_contracts(
            session,
            task,
            [(action, _payload(job.id, history="乙: 今天群里挺热闹"))],
            config={"ai_content_route_v2_enabled": True},
        )

        assert session.get(AiContentWindowPlanSlot, old_slot_id).state == "invalidated"
        assert job.window_slot_id != old_slot_id
        assert job.context_snapshot_version == 5
        assert first["_ai_content_contracts"][job.id]["window_plan_hash"] != (
            second["_ai_content_contracts"][job.id]["window_plan_hash"]
        )


def test_group_v2_terminal_replacement_gets_new_plan_revision() -> None:
    with Session(_engine()) as session:
        task, action, job = _seed(session, routes=["general"])
        first = bind_group_generation_contracts(
            session,
            task,
            [(action, _payload(job.id))],
            config={"ai_content_route_v2_enabled": True},
        )
        old_slot_id = job.window_slot_id
        job.state = "failed"
        replacement = GenerationJob(
            id="job-2",
            tenant_id=job.tenant_id,
            task_id=job.task_id,
            task_lifecycle_epoch=job.task_lifecycle_epoch,
            obligation_type=job.obligation_type,
            obligation_id=job.obligation_id,
            generation_sequence=2,
            context_snapshot_version=job.context_snapshot_version,
            context_snapshot_hash=job.context_snapshot_hash,
            state="generating",
        )
        session.add(replacement)
        session.flush()

        second = bind_group_generation_contracts(
            session,
            task,
            [(action, _payload(replacement.id))],
            config={"ai_content_route_v2_enabled": True},
            jobs=(replacement,),
        )

        assert session.get(AiContentWindowPlanSlot, old_slot_id).state == "invalidated"
        assert replacement.window_slot_id != old_slot_id
        assert first["_ai_content_contracts"][job.id]["window_plan_hash"] != (
            second["_ai_content_contracts"][replacement.id]["window_plan_hash"]
        )


def test_group_v2_batch_loads_job_and_policy_snapshots_once() -> None:
    engine = _engine()
    with Session(engine) as session:
        _task, _first_action, _first_job = _seed(session, routes=["general"])
        for index in range(2, 6):
            _add_group_item(session, index)
        session.commit()
        session.expunge_all()
        task = session.get(Task, "task-1")
        actions = session.scalars(select(Action).order_by(Action.id)).all()
        batch = [
            (action, _payload(f"job-{index}"))
            for index, action in enumerate(actions, 1)
        ]
        counts = {"binding": 0, "jobs": 0}

        def count_snapshot_reads(_connection, _cursor, statement, _params, _context, _many):
            normalized = statement.lower()
            if "from task_ai_content_policy_bindings" in normalized:
                counts["binding"] += 1
            if "from generation_jobs" in normalized:
                counts["jobs"] += 1

        event.listen(engine, "before_cursor_execute", count_snapshot_reads)
        try:
            bind_group_generation_contracts(
                session,
                task,
                batch,
                config={"ai_content_route_v2_enabled": True},
            )
        finally:
            event.remove(engine, "before_cursor_execute", count_snapshot_reads)

        assert counts == {"binding": 1, "jobs": 1}


def _seed(session: Session, *, routes: list[str]):
    task = Task(
        id="task-1",
        tenant_id=1,
        name="group-ai",
        type="group_ai_chat",
        config_revision=3,
        task_lifecycle_epoch=2,
    )
    policy = AiContentPolicyVersion(
        id="policy-1",
        tenant_id=1,
        version=1,
        status="active",
        route_rules={"allowed_routes": routes},
        prompt_registry={route: {"version": f"{route}_v1"} for route in routes},
        gate_config={
            "forbidden_claim_categories": ["price"],
            "negative_lexicon": {
                "version": "test-v1",
                "entries": [{
                    "phrase": "签到",
                    "scope": "output",
                    "routes": ["*"],
                    "match_type": "contains",
                    "enabled": True,
                }],
            },
        },
        example_set={"version": "examples-v1"},
        policy_hash="p" * 64,
    )
    adult = AdultSubjectAttestation(
        id="attestation-1",
        tenant_id=1,
        scope_type="task_group",
        scope_id="7",
        subject_class="adult_service",
        evidence_codes=["adult_service_subject_verified"],
        permission_snapshot={"adult_content_attest": True},
        expires_at=datetime(2027, 8, 19),
        task_config_revision=3,
        policy_version=1,
        status="active",
        evidence_hash="e" * 64,
    )
    attestation_ids = [adult.id] if "adult_service_sensory" in routes else []
    binding = TaskAiContentPolicyBinding(
        tenant_id=1,
        task_id=task.id,
        task_lifecycle_epoch=2,
        task_config_revision=3,
        policy_version_id=policy.id,
        allowed_routes=routes,
        attestation_ids=attestation_ids,
        evidence_hash="b" * 64,
        approved_by="reviewer",
    )
    action = Action(
        id="action-1",
        tenant_id=1,
        task_id=task.id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=9,
        scheduled_at=datetime(2026, 8, 19, 10, 20),
        pacing_due_at=datetime(2026, 8, 19, 10, 20),
        pacing_plan_hash="a" * 64,
        pacing_slot_ordinal=2,
        obligation_type="group_ai_chat",
        obligation_id="owner-1",
    )
    job = GenerationJob(
        id="job-1",
        tenant_id=1,
        task_id=task.id,
        task_lifecycle_epoch=2,
        obligation_type="group_ai_chat",
        obligation_id="owner-1",
        generation_sequence=1,
        context_snapshot_version=4,
        context_snapshot_hash="c" * 64,
        state="generating",
    )
    session.add_all((task, policy, adult, binding, action, job))
    session.flush()
    return task, action, job


def _payload(job_id: str, *, history: str = "甲: 今天群里挺热闹"):
    return SimpleNamespace(
        generation_job_id=job_id,
        group_id=7,
        ai_generation_history=history,
    )


def _add_group_item(session: Session, index: int):
    action = Action(
        id=f"action-{index}",
        tenant_id=1,
        task_id="task-1",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=index + 8,
        scheduled_at=datetime(2026, 8, 19, 10, 20 + index),
        pacing_due_at=datetime(2026, 8, 19, 10, 20 + index),
        pacing_plan_hash="a" * 64,
        pacing_slot_ordinal=index,
        obligation_type="group_ai_chat",
        obligation_id=f"owner-{index}",
    )
    job = GenerationJob(
        id=f"job-{index}",
        tenant_id=1,
        task_id="task-1",
        task_lifecycle_epoch=2,
        obligation_type="group_ai_chat",
        obligation_id=f"owner-{index}",
        generation_sequence=1,
        context_snapshot_version=4,
        context_snapshot_hash="c" * 64,
        state="generating",
    )
    session.add_all((action, job))
    session.flush()
    return action, job
