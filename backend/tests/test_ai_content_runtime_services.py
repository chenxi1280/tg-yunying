from __future__ import annotations

from datetime import datetime, timedelta
from urllib.error import HTTPError

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AiProvider,
    GenerationJob,
    Task,
    Tenant,
    TenantAiSetting,
    TenantAiProviderRouteItem,
    TenantAiProviderRouteSet,
)
from app.services.task_center.ai_content_runtime import (
    AiContentRuntimeConflict,
    ShortfallSpec,
    WindowScope,
    WindowSlotSpec,
    bump_context_revision,
    claim_window_slot,
    freeze_window_plan,
    mark_candidate_ready,
    settle_shortfall,
)
from app.services import ai_config
from app.schemas.ai_config import TenantAiSettingUpdate
from app.ai_gateway import (
    AiDraftCandidate,
    AiEmptyFinalContentError,
    AiGenerationResult,
    AiProviderCredentials,
    AiUsage,
)
from app.services.task_center import ai_provider_candidate_runtime
from app.services.task_center.ai_generation_contract import ProviderRouteDeferred
from app.services.task_center.provider_admission import ProviderAdmissionBlocked
from app.services.task_center.ai_provider_candidate_runtime import (
    ProviderCandidatePolicy,
    ProviderDraftRequest,
    generate_with_provider_candidates,
    route_transport_failure,
)
from app.services.task_center.ai_provider_routes import (
    ProviderRouteUnavailable,
    active_route_snapshot,
    bind_generation_job_routes,
    resolve_request_route,
)
from app.services.task_center.ai_generation_runtime_config import (
    _bind_legacy_provider_failover,
)


pytestmark = pytest.mark.no_postgres


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _provider(provider_id: int, model: str) -> AiProvider:
    return AiProvider(
        id=provider_id,
        provider_name=f"provider-{provider_id}",
        base_url="mock://provider",
        model_name=model,
        api_key_ciphertext="ciphertext",
        credential_enabled=True,
        is_active=provider_id == 1,
    )


def _route_set(
    session: Session,
    purpose: str,
    providers: tuple[tuple[int, str], ...],
    *,
    revision: int = 1,
) -> TenantAiProviderRouteSet:
    route_set = TenantAiProviderRouteSet(
        tenant_id=1,
        purpose=purpose,
        revision=revision,
        status="active",
        content_hash=(purpose[0] * 64)[:64],
    )
    session.add(route_set)
    session.flush()
    for priority, (provider_id, model) in enumerate(providers, 1):
        session.add(TenantAiProviderRouteItem(
            route_set_id=route_set.id,
            priority=priority,
            provider_id=provider_id,
            model_name=model,
        ))
    return route_set


def test_provider_route_preserves_explicit_priority_across_enabled_credentials() -> None:
    with Session(_engine()) as session:
        session.add_all((_provider(1, "deepseek-chat"), _provider(2, "mimo-v2")))
        _route_set(session, "group_context_route", ((2, "mimo-v2"), (1, "deepseek-chat")))
        session.commit()

        snapshot = active_route_snapshot(session, 1, "group_context_route")

        assert snapshot.provider_ids == (2, 1)
        assert snapshot.provider_models == {2: "mimo-v2", 1: "deepseek-chat"}


def test_legacy_provider_failover_binds_explicit_general_route() -> None:
    with Session(_engine()) as session:
        first = _provider(1, "deepseek-chat")
        second = _provider(2, "mimo-v2")
        second.is_active = True
        session.add_all((first, second))
        session.add(TenantAiSetting(
            tenant_id=1,
            ai_enabled=True,
            ai_provider_route_fallback_enabled=True,
        ))
        task = Task(id="legacy-route", tenant_id=1, name="legacy", type="group_ai_chat")
        session.add(task)
        _route_set(
            session,
            "group_realize_general",
            ((2, "mimo-v2"), (1, "deepseek-chat")),
        )
        session.commit()

        config = _bind_legacy_provider_failover(session, task, {})

        assert config["_ai_provider_route_provider_ids"] == [2, 1]
        assert config["provider_binding_policy"] == "explicit_provider_route"


def test_legacy_provider_failover_uses_remaining_healthy_candidate() -> None:
    with Session(_engine()) as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add(_provider(1, "deepseek-chat"))
        session.add(TenantAiSetting(
            tenant_id=1,
            ai_enabled=True,
            ai_provider_route_fallback_enabled=True,
        ))
        task = Task(id="legacy-route", tenant_id=1, name="legacy", type="group_ai_chat")
        session.add(task)
        _route_set(session, "group_realize_general", ((1, "deepseek-chat"),))
        session.commit()

        config = _bind_legacy_provider_failover(session, task, {})

        assert config["_ai_provider_route_provider_ids"] == [1]


def test_enabling_provider_route_fallback_requires_two_healthy_candidates() -> None:
    with Session(_engine()) as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add(_provider(1, "deepseek-chat"))
        session.add(TenantAiSetting(tenant_id=1, ai_enabled=True))
        _route_set(session, "group_realize_general", ((1, "deepseek-chat"),))
        session.commit()

        with pytest.raises(ValueError, match="at least two healthy active providers"):
            ai_config.update_tenant_ai_setting(
                session,
                1,
                TenantAiSettingUpdate(
                    ai_provider_route_fallback_enabled=True,
                ),
                "pytest",
            )


def test_route_bound_credentials_do_not_reuse_legacy_active_selector(monkeypatch) -> None:
    provider = _provider(2, "mimo-v2")
    provider.is_active = False
    provider.health_status = "健康"
    monkeypatch.setattr(ai_config, "decrypt_secret", lambda _value: "secret")

    credentials = ai_config.ai_provider_credentials(provider, route_bound=True)

    assert credentials.model_name == "mimo-v2"
    with pytest.raises(ValueError, match="provider disabled"):
        ai_config.ai_provider_credentials(provider)


def test_route_credentials_are_loaded_only_when_candidate_is_attempted(monkeypatch) -> None:
    providers = [_provider(1, "deepseek-chat"), _provider(2, "mimo-v2")]
    loaded: list[int] = []

    def credentials(provider, _model, *, route_bound=False):
        loaded.append(provider.id)
        if provider.id == 2:
            raise ValueError("broken standby credential")
        return object()

    monkeypatch.setattr(ai_provider_candidate_runtime, "ai_credentials", credentials)
    with Session(_engine()) as session:
        calls = ai_provider_candidate_runtime.provider_calls(
            session,
            providers,
            "default-model",
            close_transaction_before_external=False,
            route_models={1: "deepseek-chat", 2: "mimo-v2"},
        )
        first_provider, _first_credentials = next(calls)

    assert first_provider.id == 1
    assert loaded == [1]


def test_route_fallback_only_accepts_typed_transport_failures() -> None:
    assert route_transport_failure(TimeoutError("timeout")) is True
    assert route_transport_failure(HTTPError("x", 503, "unavailable", {}, None)) is True
    assert route_transport_failure(HTTPError("x", 400, "bad request", {}, None)) is False
    assert route_transport_failure(AiEmptyFinalContentError(
        "empty",
        retryable_reasoning_length=False,
    )) is False


def test_route_insufficient_balance_marks_provider_and_uses_next_candidate(
    monkeypatch,
) -> None:
    providers = [_provider(1, "deepseek-chat"), _provider(2, "mimo-v2")]
    providers[1].is_active = True
    credentials = [object(), object()]
    calls: list[int] = []
    monkeypatch.setattr(
        ai_provider_candidate_runtime,
        "draft_provider_calls",
        lambda *_args, **_kwargs: (
            providers,
            iter(zip(providers, credentials, strict=True)),
        ),
    )
    monkeypatch.setattr(
        ai_provider_candidate_runtime,
        "begin_provider_call",
        lambda *_args: None,
    )

    def generate(candidate, *_args, **_kwargs):
        calls.append(candidate.id)
        if candidate.id == 1:
            raise RuntimeError(
                'AI provider HTTP 402: {"error":{"message":"Insufficient Balance"}}'
            )
        return AiGenerationResult(
            candidates=[AiDraftCandidate("A", "备用供应商继续生成", "低")],
            usage=AiUsage(total_tokens=12),
        )

    monkeypatch.setattr(
        ai_provider_candidate_runtime,
        "generate_provider_drafts",
        generate,
    )
    request = ProviderDraftRequest(
        "prompt", 1, "topic", "tone", (), 0.7, 64, None, 30
    )
    policy = ProviderCandidatePolicy(
        "deepseek-chat",
        "",
        False,
        "群活跃续聊",
        False,
        route_provider_ids=(1, 2),
    )

    with Session(_engine()) as session:
        result = generate_with_provider_candidates(
            session, providers[0], request, policy=policy
        )

    assert calls == [1, 2]
    assert result.provider_id == 2
    assert providers[0].health_status == "异常"
    assert "Insufficient Balance" in providers[0].last_error


def test_all_route_transport_failures_raise_typed_deferred(monkeypatch) -> None:
    providers = [_provider(1, "model-a"), _provider(2, "model-b")]
    monkeypatch.setattr(
        ai_provider_candidate_runtime,
        "provider_candidates",
        lambda *_args, **_kwargs: providers,
    )
    monkeypatch.setattr(
        ai_provider_candidate_runtime,
        "provider_calls",
        lambda *_args, **_kwargs: [(item, object()) for item in providers],
    )
    monkeypatch.setattr(ai_provider_candidate_runtime, "begin_provider_call", lambda *_args: None)
    monkeypatch.setattr(
        ai_provider_candidate_runtime,
        "generate_provider_drafts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("provider timeout")),
    )
    request = ProviderDraftRequest("prompt", 1, "topic", "tone", (), 0.7, 64, None, 30)
    policy = ProviderCandidatePolicy(
        "model-a",
        "",
        False,
        "群活跃续聊",
        False,
        route_provider_ids=(1, 2),
    )

    with Session(_engine()) as session, pytest.raises(
        ProviderRouteDeferred,
        match="provider_route_deferred",
    ):
        generate_with_provider_candidates(session, providers[0], request, policy=policy)


def test_route_cooldown_switches_to_next_provider_and_records_actual_model(monkeypatch) -> None:
    providers = [_provider(1, "model-a"), _provider(2, "model-b")]
    credentials = [
        AiProviderCredentials("one", "openai_compatible", "mock://one", "model-a", "a"),
        AiProviderCredentials("two", "openai_compatible", "mock://two", "model-b", "b"),
    ]
    monkeypatch.setattr(
        ai_provider_candidate_runtime,
        "draft_provider_calls",
        lambda *_args, **_kwargs: (providers, iter(zip(providers, credentials, strict=True))),
    )

    def begin(provider):
        if provider.id == 1:
            raise ProviderAdmissionBlocked("provider-1", 30, reason="http_429")
        return None

    monkeypatch.setattr(ai_provider_candidate_runtime, "begin_provider_call", begin)
    monkeypatch.setattr(
        ai_provider_candidate_runtime,
        "generate_provider_drafts",
        lambda *_args, **_kwargs: AiGenerationResult(
            [AiDraftCandidate("群友", "第二供应商成功")],
            AiUsage(),
        ),
    )
    policy = ProviderCandidatePolicy(
        "model-a", "", False, "群活跃续聊", False, route_provider_ids=(1, 2),
    )
    request = ProviderDraftRequest("prompt", 1, "topic", "tone", (), 0.7, 64, None, 30)

    with Session(_engine()) as session:
        result = generate_with_provider_candidates(session, providers[0], request, policy=policy)

    assert result.provider_id == 2
    assert result.model_name == "model-b"
    assert [item["outcome"] for item in result.provider_attempts] == ["failed", "success"]


def test_reviewer_route_rejects_any_generator_identity_overlap() -> None:
    with Session(_engine()) as session:
        session.add_all((_provider(1, "deepseek-chat"), _provider(2, "mimo-v2")))
        _route_set(session, "group_context_route", ((2, "mimo-v2"),))
        _route_set(session, "group_realize_general", ((1, "deepseek-chat"),))
        _route_set(session, "group_semantic_review", ((1, "deepseek-chat"),))
        session.commit()

        with pytest.raises(ProviderRouteUnavailable, match="must_differ"):
            resolve_request_route(
                session,
                1,
                "两阶段语义审核",
                config={"ai_content_route_v2_enabled": True},
            )


def test_generation_job_route_snapshot_survives_active_route_switch() -> None:
    now = datetime(2026, 8, 19, 10, 0, 0)
    with Session(_engine()) as session:
        session.add_all((
            _provider(1, "deepseek-chat"),
            _provider(2, "review-model"),
            _provider(3, "mimo-v2"),
        ))
        original = _route_set(
            session,
            "comment_context_route",
            ((1, "deepseek-chat"),),
        )
        _route_set(session, "comment_realize_general", ((1, "deepseek-chat"),))
        _route_set(session, "comment_semantic_review", ((2, "review-model"),))
        job = _job(now)
        session.add(job)
        session.commit()

        config = bind_generation_job_routes(
            session,
            (job,),
            {"ai_content_route_v2_enabled": True},
            scope_type="comment",
        )
        original.status = "retired"
        replacement = _route_set(
            session,
            "comment_context_route",
            ((3, "mimo-v2"),),
            revision=2,
        )
        session.commit()

        snapshot = resolve_request_route(
            session,
            1,
            "两阶段意图规划",
            config=config,
        )

        assert snapshot is not None
        assert snapshot.route_set_id == original.id
        assert snapshot.provider_ids == (1,)
        assert job.provider_route_set_id == original.id


def test_generation_job_route_binding_reuses_one_batch_snapshot() -> None:
    now = datetime(2026, 8, 19, 10, 0, 0)
    engine = _engine()
    with Session(engine) as session:
        session.add_all((_provider(1, "deepseek-chat"), _provider(2, "review-model")))
        _route_set(session, "comment_context_route", ((1, "deepseek-chat"),))
        _route_set(session, "comment_realize_general", ((1, "deepseek-chat"),))
        _route_set(session, "comment_semantic_review", ((2, "review-model"),))
        jobs = tuple(_job(now + timedelta(seconds=index)) for index in range(5))
        for index, job in enumerate(jobs, 1):
            job.id = f"job-{index}"
            job.obligation_id = f"owner-{index}"
        session.add_all(jobs)
        session.flush()
        select_count = 0

        def count_selects(_connection, _cursor, statement, _params, _context, _many):
            nonlocal select_count
            if statement.lstrip().upper().startswith("SELECT"):
                select_count += 1

        event.listen(engine, "before_cursor_execute", count_selects)
        try:
            bind_generation_job_routes(
                session,
                jobs,
                {"ai_content_route_v2_enabled": True},
                scope_type="comment",
            )
        finally:
            event.remove(engine, "before_cursor_execute", count_selects)

        assert select_count <= 10
        assert all(job.provider_route_snapshots == jobs[0].provider_route_snapshots for job in jobs)


def test_context_revision_is_monotonic_and_window_slot_is_independent() -> None:
    now = datetime(2026, 8, 19, 10, 0, 0)
    with Session(_engine()) as session:
        first = bump_context_revision(
            session,
            tenant_id=1,
            scope_type="group",
            scope_id="7",
            snapshot_hash="a" * 64,
            human_message_id="10",
        )
        second = bump_context_revision(
            session,
            tenant_id=1,
            scope_type="group",
            scope_id="7",
            snapshot_hash="b" * 64,
            human_message_id="11",
        )
        assert first.id == second.id
        assert second.context_scope_revision == 2

        plan = freeze_window_plan(
            session,
            _scope(now),
            (_slot(now, second.context_scope_revision),),
        )
        job = _job(now)
        session.add(job)
        session.flush()
        claimed = claim_window_slot(session, job, lease_duration=timedelta(minutes=2))
        assert claimed.claimed_by_job_id == job.id
        assert job.window_plan_hash == plan.plan_hash
        assert job.context_snapshot_version == 2

        mark_candidate_ready(session, job, candidate_hash="c" * 64)
        session.flush()
        assert claimed.state == "candidate_ready"
        assert job.candidate_hash == "c" * 64


def test_context_revision_preserves_fields_that_are_not_updated() -> None:
    with Session(_engine()) as session:
        first = bump_context_revision(
            session,
            tenant_id=1,
            scope_type="group",
            scope_id="7",
            snapshot_hash="a" * 64,
            reply_target_hash="r" * 64,
        )
        assert first.reply_target_hash == "r" * 64

        second = bump_context_revision(
            session,
            tenant_id=1,
            scope_type="group",
            scope_id="7",
            snapshot_hash="b" * 64,
            human_message_id="42",
        )

        assert second.last_human_message_id == "42"
        assert second.reply_target_hash == "r" * 64


def test_shortfall_is_unique_per_owner_and_conflicting_evidence_fails() -> None:
    with Session(_engine()) as session:
        spec = _shortfall("a" * 64)
        first = settle_shortfall(session, spec)
        second = settle_shortfall(session, spec)
        assert first.id == second.id
        with pytest.raises(AiContentRuntimeConflict, match="identity_conflict"):
            settle_shortfall(session, _shortfall("b" * 64))


def _scope(now: datetime) -> WindowScope:
    return WindowScope(
        tenant_id=1,
        task_id="task-1",
        task_lifecycle_epoch=1,
        scope_type="group",
        scope_id="7",
        pacing_plan_hash="p" * 64,
        period_key="2026-08-19",
        window_start_at=now,
        window_end_at=now + timedelta(hours=1),
        task_config_revision=3,
        content_policy_hash="q" * 64,
    )


def _slot(now: datetime, revision: int) -> WindowSlotSpec:
    return WindowSlotSpec(
        slot_ordinal=1,
        obligation_type="group_ai_chat",
        obligation_id="owner-1",
        generation_sequence=1,
        account_id=9,
        due_at=now + timedelta(minutes=20),
        context_scope_revision=revision,
        context_snapshot_hash="b" * 64,
        context_route="general",
        content_mode="general",
        route_evidence_hash="e" * 64,
        prompt_contract_version="general_v2",
    )


def _job(now: datetime) -> GenerationJob:
    return GenerationJob(
        tenant_id=1,
        task_id="task-1",
        obligation_type="group_ai_chat",
        obligation_id="owner-1",
        generation_sequence=1,
        context_snapshot_version=1,
        generation_not_before_at=now,
        state="generating",
    )


def _shortfall(evidence_hash: str) -> ShortfallSpec:
    return ShortfallSpec(
        tenant_id=1,
        task_id="task-1",
        task_lifecycle_epoch=1,
        owner_type="group_ai_chat",
        owner_id="owner-1",
        period_key="2026-08-19",
        kind="quality",
        reason_code="semantic_review_failed",
        requested_quantity=1,
        settled_quantity=0,
        evidence_hash=evidence_hash,
    )
