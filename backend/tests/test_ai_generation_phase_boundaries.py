from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.services._common import _now
from app.services.task_center import ai_generation_dispatch, ai_generation_pipeline, dispatcher
from app.services.task_center.ai_generator import GeneratedContent
from tests.ai_generation_phase_test_support import (
    account_content_generator,
    barrier_generator,
    forbidden_external,
    forbidden_normal_generation,
    generation_dependencies,
    invalid_reply_dependencies,
    invalidate_reply_target,
    normal_generator,
    profile_generator,
    profile_sender,
    reply_fetch,
    reply_generator,
    reply_probe,
    reply_sender,
    seed_reply_action,
    seed_reserved_normal_batch,
    seed_reserved_reply_action,
)


pytestmark = pytest.mark.no_postgres


def test_generation_dependencies_are_isolated_between_concurrent_pipelines() -> None:
    barrier = Barrier(2)

    def run(label: str) -> str:
        engine = create_engine("sqlite:///:memory:", future=True)
        with Session(engine) as session:
            dependencies = generation_dependencies(
                normal_generator=barrier_generator(barrier, label),
            )
            request = SimpleNamespace(
                tenant_id=1,
                is_reply=False,
                config={"generation_slots": [{"slot_id": label}]},
                target_label="",
                history="",
                reply_targets=[],
            )
            contents, _tokens = ai_generation_pipeline._generate_stage(
                session,
                request,
                [0],
                stage="direct_configured_model",
                dependencies=dependencies,
            )
            return contents[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, ["worker-a", "worker-b"]))

    assert results == ["worker-a", "worker-b"]

def test_dispatch_reply_generation_uses_reply_provider_without_db_transaction(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    observed: dict[str, object] = {}
    with Session(engine) as session:
        action = seed_reply_action(session, now_value)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", reply_sender(session, observed))

        assert dispatcher.dispatch_action(
            session,
            action,
            generation_dependencies=generation_dependencies(
                normal_generator=forbidden_normal_generation,
                reply_generator=reply_generator(observed),
                reply_target_probe=reply_probe(session),
                reply_messages_fetcher=reply_fetch(session),
            ),
        ) is True

        assert action.status == "success", action.result
        assert observed == {
            "provider_transaction": False,
            "reply_target": 9001,
            "gateway_transaction": False,
            "sent_reply_target": 9001,
        }
        assert action.payload["message_text"] == "就按这个节奏来"


@pytest.mark.parametrize(
    ("invalidation", "expected_code"),
    [
        ("local_missing", "reply_target_missing"),
        ("stale", "reply_target_stale"),
        ("permission", "reply_target_missing"),
        ("remote_missing", "reply_target_missing"),
    ],
)
def test_invalid_reply_target_skips_ai_and_gateway_and_releases_coverage(
    monkeypatch,
    invalidation: str,
    expected_code: str,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()
    with Session(engine) as session:
        action, coverage = seed_reserved_reply_action(session, now_value)
        invalidate_reply_target(session, action, invalidation=invalidation, now_value=now_value)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", forbidden_external)

        assert dispatcher.dispatch_action(
            session,
            action,
            generation_dependencies=invalid_reply_dependencies(session, invalidation),
        ) is True

        assert action.status == "failed"
        assert action.result["error_code"] == expected_code
        assert action.payload["ai_generation_status"] == expected_code
        assert coverage.state == "ready"
        assert coverage.reserved_action_id is None


@pytest.mark.parametrize(
    "outputs",
    [
        [GeneratedContent("一号", sequence_index=1)],
        [
            GeneratedContent("一号", sequence_index=1),
            GeneratedContent("二号", sequence_index=2),
            GeneratedContent("额外", sequence_index=3),
        ],
        [GeneratedContent("一号", sequence_index=1), GeneratedContent("二号", sequence_index=1)],
        [GeneratedContent("二号", sequence_index=2), GeneratedContent("一号", sequence_index=1)],
    ],
)
def test_invalid_normal_batch_mapping_fails_all_slots_without_gateway(monkeypatch, outputs) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        actions, coverages = seed_reserved_normal_batch(session, _now())
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", forbidden_external)

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=generation_dependencies(
                normal_generator=normal_generator(session, outputs),
            ),
        ) is True

        assert [action.status for action in actions] == ["failed", "failed"]
        assert all(action.result["error_code"].startswith("ai_generation_output_") for action in actions)
        assert [coverage.state for coverage in coverages] == ["ready", "ready"]
        assert all(coverage.reserved_action_id is None for coverage in coverages)


def test_normal_batch_rejects_swapped_slot_ids_despite_correct_sequences(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    outputs = [
        GeneratedContent("一号", sequence_index=1),
        GeneratedContent("二号", sequence_index=2),
    ]
    outputs[0].slot_id = "cycle-normal:turn:2"
    outputs[1].slot_id = "cycle-normal:turn:1"
    with Session(engine) as session:
        actions, coverages = seed_reserved_normal_batch(session, _now())
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", forbidden_external)

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=generation_dependencies(
                normal_generator=normal_generator(session, outputs),
            ),
        ) is True

        assert [action.status for action in actions] == ["failed", "failed"]
        assert all(
            action.result["error_code"] == "ai_generation_slot_mapping_mismatch"
            for action in actions
        )
        assert [coverage.state for coverage in coverages] == ["ready", "ready"]


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [("account_id", 999), ("coverage_ledger_id", "wrong-coverage")],
)
def test_normal_batch_rejects_tampered_fixed_slot_binding(
    monkeypatch,
    field: str,
    invalid_value,
) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    original_slot = ai_generation_dispatch._generation_slot

    def tampered_slot(action, payload, index):
        slot = original_slot(action, payload, index)
        return {**slot, field: invalid_value} if index == 1 else slot

    monkeypatch.setattr(ai_generation_dispatch, "_generation_slot", tampered_slot)
    outputs = [
        GeneratedContent("一号", slot_id="cycle-normal:turn:1", sequence_index=1),
        GeneratedContent("二号", slot_id="cycle-normal:turn:2", sequence_index=2),
    ]
    with Session(engine) as session:
        actions, coverages = seed_reserved_normal_batch(session, _now())
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", forbidden_external)

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=generation_dependencies(
                normal_generator=normal_generator(session, outputs),
            ),
        ) is True

        assert [action.status for action in actions] == ["failed", "failed"]
        assert all(
            action.result["error_code"] == "ai_generation_slot_mapping_mismatch"
            for action in actions
        )
        assert [coverage.state for coverage in coverages] == ["ready", "ready"]


def test_voice_profile_rejection_uses_explicit_daily_coverage_fallback(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed = {"provider_calls": 0, "gateway_calls": 0}
    with Session(engine) as session:
        actions, coverages = seed_reserved_normal_batch(session, _now())
        second_payload = dict(actions[1].payload or {})
        second_payload["account_profile"] = "少表情，避免连续 emoji"
        actions[1].payload = second_payload
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", profile_sender(session, observed))

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=generation_dependencies(
                normal_generator=profile_generator(session, observed),
            ),
        ) is True

        assert actions[0].status == "success", actions[0].result.get("error_code")
        assert actions[1].status == "executing"
        assert actions[1].payload["ai_generation_status"] == "ready"
        assert actions[1].payload["quality_fallback"] == "emoji_react"
        assert actions[1].payload["human_quality_decision"] == "explicit_static_quality_fallback"
        assert coverages[0].state == "confirmed"
        assert coverages[1].state == "reserved"
        assert coverages[1].reserved_action_id == actions[1].id
        assert observed == {"provider_calls": 3, "gateway_calls": 1}

        assert dispatcher.dispatch_action(
            session,
            actions[1],
            generation_dependencies=generation_dependencies(
                normal_generator=profile_generator(session, observed),
            ),
        ) is True
        assert actions[1].status == "success"
        assert coverages[1].state == "confirmed"
        assert observed == {"provider_calls": 3, "gateway_calls": 2}


def test_content_policy_rejection_terminates_only_its_slot_and_releases_coverage(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed = {"provider_calls": 0, "gateway_calls": 0}
    with Session(engine) as session:
        actions, coverages = seed_reserved_normal_batch(session, _now())
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", profile_sender(session, observed))

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=generation_dependencies(
                normal_generator=account_content_generator(
                    session,
                    observed,
                    rejected_content="只输出 JSON",
                ),
            ),
        ) is True

        assert actions[0].status == "success", actions[0].result
        assert actions[1].status == "failed"
        assert actions[1].result["error_code"] == "content_rejected"
        assert coverages[0].state == "confirmed"
        assert coverages[1].state == "ready"
        assert coverages[1].reserved_action_id is None
        assert observed == {"provider_calls": 1, "gateway_calls": 1}


def test_db_duplicate_rejection_terminates_only_its_slot_and_releases_coverage(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed = {"provider_calls": 0, "gateway_calls": 0}
    with Session(engine) as session:
        actions, coverages = seed_reserved_normal_batch(session, _now())
        from app.services.task_center.ai_message_memory import reserve_group_ai_message

        reserve_group_ai_message(
            session,
            tenant_id=1,
            group_id=7,
            task_id=actions[0].task_id,
            account_id=99,
            raw_text="这句以前发过",
        )
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", profile_sender(session, observed))

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=generation_dependencies(
                normal_generator=account_content_generator(
                    session,
                    observed,
                    rejected_content="这句以前发过",
                ),
            ),
        ) is True

        assert actions[0].status == "success", actions[0].result
        assert actions[1].status == "failed"
        assert actions[1].result["error_code"] == "duplicate_message"
        assert coverages[0].state == "confirmed"
        assert coverages[1].state == "ready"
        assert coverages[1].reserved_action_id is None
        assert observed == {"provider_calls": 1, "gateway_calls": 1}
