from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.ai_gateway import AiProviderRateLimited
from app.database import Base
from app.models import GenerationJob, TaskRuntimeActiveBlocker
from app.services.task_center.ai_generator import GROUP_CHAT_PURPOSE
from app.services.task_center.provider_admission import (
    ProviderAdmissionBlocked,
    ProviderAdmissionUnavailable,
    begin_provider_call,
    extend_provider_cooldown,
    provider_admission_key,
    release_provider_probe,
)
from tests.test_provider_admission import (
    BrokenAdmissionRedis,
    FakeAdmissionRedis,
    _enable_admission,
    _provider,
)


pytestmark = pytest.mark.no_postgres


# generator pre-call fence
# ---------------------------------------------------------------------------


def _provider_draft_request(ai_generator):  # noqa: ANN001
    return ai_generator.ProviderDraftRequest(
        "prompt", 1, "t", "t", ("p",), 0.8, 128, None, 30,
    )


def _provider_candidate_policy(ai_generator):  # noqa: ANN001
    return ai_generator.ProviderCandidatePolicy(
        "", "", False, GROUP_CHAT_PURPOSE, False,
    )


def test_generate_candidates_on_429_extends_cooldown_and_defers(monkeypatch):
    from app.services.task_center import ai_provider_candidate_runtime as ai_generator

    fake = FakeAdmissionRedis()
    _enable_admission(monkeypatch, fake)
    provider = _provider()

    calls = {"count": 0}
    admission_events: list[str] = []

    def _extend(*args, **kwargs):  # noqa: ANN002, ANN003
        admission_events.append("extend")
        return extend_provider_cooldown(*args, **kwargs)

    def _release(lease):  # noqa: ANN001
        admission_events.append("release")
        return release_provider_probe(lease)

    def _rate_limited(*_args, **_kwargs):
        calls["count"] += 1
        raise AiProviderRateLimited(429, "rate limited by provider", 5)

    monkeypatch.setattr(ai_generator, "ai_provider_credentials", lambda _p, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(ai_generator.ai_gateway, "generate_drafts", _rate_limited)
    monkeypatch.setattr(ai_generator, "extend_provider_cooldown", _extend)
    monkeypatch.setattr(ai_generator, "release_provider_probe", _release)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(provider)
        session.commit()

        with pytest.raises(ProviderAdmissionBlocked) as exc_info:
            ai_generator.generate_with_provider_candidates(
                session,
                provider,
                _provider_draft_request(ai_generator),
                policy=_provider_candidate_policy(ai_generator),
            )

        assert exc_info.value.reason == "provider_rate_limited"
        assert calls["count"] == 1  # 单次结算，不换 provider 立即重发
        assert admission_events == ["extend", "release"]

        state = fake.hash[provider_admission_key(provider)]
        assert state["source_status"] == "cooldown"
        assert float(state["retry_at"]) >= time.time() + 4
        # probe 已释放，cooldown 期间后续调用直接被拦截
        with pytest.raises(ProviderAdmissionBlocked):
            begin_provider_call(provider)


def test_generate_candidates_success_settles_open_marker(monkeypatch):
    from app.services.task_center import ai_provider_candidate_runtime as ai_generator

    fake = FakeAdmissionRedis()
    _enable_admission(monkeypatch, fake)
    provider = _provider()
    monkeypatch.setattr(ai_generator, "ai_provider_credentials", lambda _p, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(
        ai_generator.ai_gateway,
        "generate_drafts",
        lambda *_args, **_kwargs: SimpleNamespace(
            candidates=[SimpleNamespace(content="你好呀")],
            usage=SimpleNamespace(total_tokens=3),
        ),
    )
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(provider)
        session.commit()

        result = ai_generator.generate_with_provider_candidates(
            session,
            provider,
            _provider_draft_request(ai_generator),
            policy=_provider_candidate_policy(ai_generator),
        )

        assert result.candidates[0].content == "你好呀"
        state = fake.hash[provider_admission_key(provider)]
        assert state["source_status"] == "open"
        assert fake.strings == {}  # probe 已释放


# ---------------------------------------------------------------------------
# claim fence integration (parallel claim)
# ---------------------------------------------------------------------------


def test_claim_parallel_generation_stops_when_all_providers_cooldown(monkeypatch):
    from app.services.task_center import ai_generation_parallel
    from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION
    from tests.ai_generation_phase_test_support import seed_reserved_normal_batch
    from app.services._common import _now

    fake = FakeAdmissionRedis()
    _enable_admission(monkeypatch, fake)
    provider = _provider()
    fake.hash[provider_admission_key(provider)] = {
        "retry_at": repr(time.time() + 120),
        "reason": "http_429",
        "source_status": "cooldown",
        "version": "1",
    }
    claimed_action_ids: list[str] = []

    def _fake_claim_one(_session, action, _owner):
        claimed_action_ids.append(action.id)
        return None

    monkeypatch.setattr(ai_generation_parallel, "_claim_one", _fake_claim_one)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(provider)
        actions, _coverages = seed_reserved_normal_batch(session, _now(), bind_coverage=False)
        from app.models import Task

        task = session.get(Task, actions[0].task_id)
        task.fulfillment_contract_version = CURRENT_CONTRACT_VERSION
        for action in actions:
            action.status = "pending"
            action.claim_owner = ""
            action.claim_token = ""
        session.commit()
        session_factory = lambda: Session(session.get_bind())  # noqa: E731

        claims = ai_generation_parallel.claim_parallel_generation(
            session_factory,
            owner="worker-a",
            limit=5,
        )

        assert claims == ()
        # cooldown 生效时在领取前停止：不尝试 claim 任何 action
        assert claimed_action_ids == []
        assert session.query(GenerationJob).count() == 0
        for action in actions:
            session.refresh(action)
            assert action.status == "pending"


def test_claim_parallel_generation_resumes_after_cooldown_expires(monkeypatch):
    from app.services.task_center import ai_generation_parallel
    from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION
    from tests.ai_generation_phase_test_support import seed_reserved_normal_batch
    from app.services._common import _now

    fake = FakeAdmissionRedis()
    _enable_admission(monkeypatch, fake)
    provider = _provider()
    fake.hash[provider_admission_key(provider)] = {
        "retry_at": repr(time.time() - 1),
        "reason": "http_429",
        "source_status": "cooldown",
        "version": "1",
    }
    claimed_action_ids: list[str] = []

    def _fake_claim_one(_session, action, _owner):
        claimed_action_ids.append(action.id)
        return None

    monkeypatch.setattr(ai_generation_parallel, "_claim_one", _fake_claim_one)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(provider)
        actions, _coverages = seed_reserved_normal_batch(session, _now(), bind_coverage=False)
        from app.models import Task

        task = session.get(Task, actions[0].task_id)
        task.fulfillment_contract_version = CURRENT_CONTRACT_VERSION
        for action in actions:
            action.status = "pending"
            action.claim_owner = ""
            action.claim_token = ""
        session.commit()
        session_factory = lambda: Session(session.get_bind())  # noqa: E731

        ai_generation_parallel.claim_parallel_generation(
            session_factory,
            owner="worker-a",
            limit=5,
        )

        assert set(claimed_action_ids) == {action.id for action in actions}


def test_claim_parallel_generation_projects_unavailable_blocker(monkeypatch):
    from app.services.task_center import ai_generation_parallel
    from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION
    from tests.ai_generation_phase_test_support import seed_reserved_normal_batch
    from app.services._common import _now
    from app.models import Task

    _enable_admission(monkeypatch, BrokenAdmissionRedis())
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(_provider())
        actions, _coverages = seed_reserved_normal_batch(
            session,
            _now(),
            bind_coverage=False,
        )
        task = session.get(Task, actions[0].task_id)
        task.fulfillment_contract_version = CURRENT_CONTRACT_VERSION
        for action in actions:
            action.status = "pending"
            action.claim_owner = ""
            action.claim_token = ""
        session.commit()

        with pytest.raises(ProviderAdmissionUnavailable):
            ai_generation_parallel.claim_parallel_generation(
                lambda: Session(session.get_bind()),
                owner="worker-a",
                limit=5,
            )

        for action in actions:
            session.refresh(action)
        session.refresh(task)
        marked = [
            action
            for action in actions
            if (action.result or {}).get("error_code") == "provider_admission_unavailable"
        ]
        assert len(marked) == 1
        assert marked[0].status == "pending"
        _assert_provider_blocker(session, task.id)


def test_claim_parallel_generation_checks_admission_once_before_batch(monkeypatch):
    from app.models import Task
    from app.services._common import _now
    from app.services.task_center import ai_generation_parallel
    from app.services.task_center.fulfillment_activation import CURRENT_CONTRACT_VERSION
    from tests.ai_generation_phase_test_support import seed_reserved_normal_batch

    calls = {"count": 0}

    def _count_admission(_session):
        calls["count"] += 1

    monkeypatch.setattr(ai_generation_parallel, "ensure_claim_admission", _count_admission)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(
            session,
            _now(),
            bind_coverage=False,
        )
        task = session.get(Task, actions[0].task_id)
        task.fulfillment_contract_version = CURRENT_CONTRACT_VERSION
        _pending_legacy_actions(session, actions)

        claims = ai_generation_parallel.claim_parallel_generation(
            lambda: Session(session.get_bind()),
            owner="worker-a",
            limit=5,
        )

        for action in actions:
            session.refresh(action)
        assert calls["count"] == 1
        assert len(claims) == len(actions)
        assert all(action.status == "executing" for action in actions)


# ---------------------------------------------------------------------------
# worker release semantics（已领取、未发 HTTP 的 job 释放 lease）
# ---------------------------------------------------------------------------


def _pending_legacy_actions(session, actions):
    for action in actions:
        action.status = "pending"
        action.claim_owner = ""
        action.claim_token = ""
        action.executed_at = None
    session.commit()


def test_drain_releases_claim_and_job_on_provider_admission_blocked():
    from app.services.task_center.ai_generation_worker import drain_ai_generation
    from tests.ai_generation_phase_test_support import seed_reserved_normal_batch
    from app.services._common import _now

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, _now(), bind_coverage=False)
        _pending_legacy_actions(session, actions)

        def blocked_processor(_session, action, _account):
            raise ProviderAdmissionBlocked("ai:provider:admission:v1:test", 30)

        processed = drain_ai_generation(
            lambda: Session(session.get_bind()),
            limit=1,
            generate_action=blocked_processor,
        )
        session.refresh(actions[0])

        assert processed == 1
        assert actions[0].status == "pending"
        assert actions[0].claim_owner == ""
        assert actions[0].payload["ai_generation_status"] == "pending"
        # 不得写 generation failed / 不得创建空正文 Action
        assert not (actions[0].result or {}).get("error_code")
        assert not str(actions[0].payload.get("message_text") or "").strip()


def test_drain_stops_and_releases_on_provider_admission_unavailable():
    from app.services.task_center.ai_generation_worker import drain_ai_generation
    from tests.ai_generation_phase_test_support import seed_reserved_normal_batch
    from app.services._common import _now
    from app.models import Task

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        actions, _coverages = seed_reserved_normal_batch(session, _now(), bind_coverage=False)
        _pending_legacy_actions(session, actions)

        def unavailable_processor(_session, action, _account):
            raise ProviderAdmissionUnavailable("redis down")

        processed = drain_ai_generation(
            lambda: Session(session.get_bind()),
            limit=1,
            generate_action=unavailable_processor,
        )
        session.refresh(actions[0])

        assert processed == 0
        assert actions[0].status == "pending"
        assert actions[0].claim_owner == ""
        assert actions[0].result["error_code"] == "provider_admission_unavailable"
        task = session.get(Task, actions[0].task_id)
        _assert_provider_blocker(session, task.id)


def _assert_provider_blocker(session: Session, task_id: str) -> None:
    blocker = session.scalar(select(TaskRuntimeActiveBlocker).where(
        TaskRuntimeActiveBlocker.task_id == task_id,
        TaskRuntimeActiveBlocker.blocker_domain == "conversation_quality",
    ))
    assert blocker.blocker_code == "provider_admission_unavailable"
