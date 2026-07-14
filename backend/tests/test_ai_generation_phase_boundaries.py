from __future__ import annotations

from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import OperationResult, SendResult
from app.models import (
    Action,
    GroupContextMessage,
    Task,
    TaskAccountDailyCoverage,
    Tenant,
    TgAccount,
    TgAccountOnlineState,
    TgGroup,
    TgGroupAccount,
)
from app.services._common import _now
from app.services.task_center import ai_generation_dispatch, ai_generation_pipeline, dispatcher
from app.services.task_center.ai_generation_dependencies import GenerationDependencies
from app.services.task_center.ai_generator import GeneratedContent
from app.services.task_center.ai_message_memory import reserve_group_ai_message


pytestmark = pytest.mark.no_postgres


def test_generation_dependencies_are_isolated_between_concurrent_pipelines() -> None:
    barrier = Barrier(2)

    def run(label: str) -> str:
        engine = create_engine("sqlite:///:memory:", future=True)
        with Session(engine) as session:
            dependencies = _generation_dependencies(
                normal_generator=_barrier_generator(barrier, label),
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
        action = _seed_reply_action(session, now_value)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", _reply_sender(session, observed))

        assert dispatcher.dispatch_action(
            session,
            action,
            generation_dependencies=_generation_dependencies(
                normal_generator=_forbidden_normal_generation,
                reply_generator=_reply_generator(observed),
                reply_target_probe=_reply_probe(session),
                reply_messages_fetcher=_reply_fetch(session),
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
        action, coverage = _seed_reserved_reply_action(session, now_value)
        _invalidate_reply_target(session, action, invalidation, now_value)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", _forbidden_external)

        assert dispatcher.dispatch_action(
            session,
            action,
            generation_dependencies=_invalid_reply_dependencies(session, invalidation),
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
        actions, coverages = _seed_reserved_normal_batch(session, _now())
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", _forbidden_external)

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=_generation_dependencies(
                normal_generator=_normal_generator(session, outputs),
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
        actions, coverages = _seed_reserved_normal_batch(session, _now())
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", _forbidden_external)

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=_generation_dependencies(
                normal_generator=_normal_generator(session, outputs),
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
        actions, coverages = _seed_reserved_normal_batch(session, _now())
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", _forbidden_external)

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=_generation_dependencies(
                normal_generator=_normal_generator(session, outputs),
            ),
        ) is True

        assert [action.status for action in actions] == ["failed", "failed"]
        assert all(
            action.result["error_code"] == "ai_generation_slot_mapping_mismatch"
            for action in actions
        )
        assert [coverage.state for coverage in coverages] == ["ready", "ready"]


def test_voice_profile_rejection_terminates_only_its_slot_and_releases_coverage(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed = {"provider_calls": 0, "gateway_calls": 0}
    with Session(engine) as session:
        actions, coverages = _seed_reserved_normal_batch(session, _now())
        second_payload = dict(actions[1].payload or {})
        second_payload["account_profile"] = "少表情，避免连续 emoji"
        actions[1].payload = second_payload
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", _profile_sender(session, observed))

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=_generation_dependencies(
                normal_generator=_profile_generator(session, observed),
            ),
        ) is True

        assert actions[0].status == "success", actions[0].result.get("error_code")
        assert actions[1].status == "failed"
        assert actions[1].result["error_code"] == "voice_profile_mismatch"
        assert coverages[0].state == "confirmed"
        assert coverages[1].state == "ready"
        assert coverages[1].reserved_action_id is None
        assert observed == {"provider_calls": 3, "gateway_calls": 1}


def test_content_policy_rejection_terminates_only_its_slot_and_releases_coverage(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed = {"provider_calls": 0, "gateway_calls": 0}
    with Session(engine) as session:
        actions, coverages = _seed_reserved_normal_batch(session, _now())
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(dispatcher.gateway, "send_message", _profile_sender(session, observed))

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=_generation_dependencies(
                normal_generator=_account_content_generator(
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
        actions, coverages = _seed_reserved_normal_batch(session, _now())
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
        monkeypatch.setattr(dispatcher.gateway, "send_message", _profile_sender(session, observed))

        assert dispatcher.dispatch_action(
            session,
            actions[0],
            generation_dependencies=_generation_dependencies(
                normal_generator=_account_content_generator(
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


def _normal_generator(session: Session, outputs: list[GeneratedContent]):
    def generate(*_args, **_kwargs):
        assert session.in_transaction() is False
        return outputs, 7

    return generate


def _barrier_generator(barrier: Barrier, label: str):
    def generate(_session, _tenant_id, config, **_kwargs):
        barrier.wait(timeout=5)
        return [GeneratedContent(
            label,
            slot_id=config["generation_slots"][0]["slot_id"],
            sequence_index=1,
        )], 1

    return generate


def _profile_generator(session: Session, observed: dict[str, int]):
    def generate(_session, _tenant_id, config, *, count, **_kwargs):
        assert session.in_transaction() is False
        observed["provider_calls"] += 1
        slots = list(config["generation_slots"])
        contents = ["😂😂" if int(slot["account_id"]) == 12 else "今天先签到" for slot in slots]
        return [
            GeneratedContent(content, slot_id=slot["slot_id"], sequence_index=index)
            for index, (slot, content) in enumerate(zip(slots, contents, strict=True), 1)
        ], count

    return generate


def _account_content_generator(session: Session, observed: dict[str, int], *, rejected_content: str):
    def generate(_session, _tenant_id, config, *, count, **_kwargs):
        assert session.in_transaction() is False
        observed["provider_calls"] += 1
        slots = list(config["generation_slots"])
        contents = [rejected_content if int(slot["account_id"]) == 12 else "今天先签到" for slot in slots]
        return [
            GeneratedContent(content, slot_id=slot["slot_id"], sequence_index=index)
            for index, (slot, content) in enumerate(zip(slots, contents, strict=True), 1)
        ], count

    return generate


def _profile_sender(session: Session, observed: dict[str, int]):
    def send(*_args, **_kwargs):
        assert session.in_transaction() is False
        observed["gateway_calls"] += 1
        return SendResult(True, remote_message_id="tg-profile-1")

    return send


def _seed_reply_action(session: Session, now_value):
    context = _seed_reply_scope(session, now_value)
    action = Action(
        id="action-reply-generation",
        tenant_id=1,
        task_id="task-reply-generation",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status="executing",
        scheduled_at=now_value,
        payload=_reply_payload(context.id),
    )
    session.add(action)
    session.commit()
    return action


def _seed_reply_scope(session: Session, now_value) -> GroupContextMessage:
    session.add(Tenant(id=1, name="tenant"))
    session.add(Task(
        id="task-reply-generation",
        tenant_id=1,
        name="reply",
        type="group_ai_chat",
        status="running",
        type_config={"target_group_id": 7, "context_bound_schedule_window_seconds": 300},
    ))
    session.add(TgAccount(
        id=11,
        tenant_id=1,
        display_name="账号A",
        phone_masked="+8611",
        status="在线",
        session_ciphertext="session-a",
    ))
    session.add(TgAccountOnlineState(
        tenant_id=1,
        account_id=11,
        desired_online=True,
        online_status="online",
        last_seen_at=now_value,
        stale_after_at=now_value + timedelta(minutes=10),
    ))
    session.add(TgGroup(
        id=7,
        tenant_id=1,
        tg_peer_id="-1007",
        title="运营群",
        auth_status="已授权运营",
        can_send=True,
        require_review=False,
    ))
    session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True))
    context = GroupContextMessage(
        tenant_id=1,
        group_id=7,
        listener_account_id=11,
        sender_name="真人用户",
        content="今天按原计划吗？",
        remote_message_id="9001",
        sent_at=now_value - timedelta(minutes=1),
    )
    session.add(context)
    session.flush()
    return context


def _seed_reserved_reply_action(session: Session, now_value):
    action = _seed_reply_action(session, now_value)
    coverage = TaskAccountDailyCoverage(
        id="coverage-reply-generation",
        tenant_id=1,
        task_id=action.task_id,
        group_id=7,
        account_id=11,
        coverage_date=now_value.date(),
        state="reserved",
        reserved_action_id=action.id,
        targeted_at=now_value,
    )
    session.add(coverage)
    payload = dict(action.payload or {})
    payload["coverage_ledger_id"] = coverage.id
    payload["account_coverage_mode"] = "all_accounts_daily"
    action.payload = payload
    session.commit()
    return action, coverage


def _seed_reserved_normal_batch(session: Session, now_value):
    first = _seed_reply_action(session, now_value)
    session.add(TgAccount(
        id=12,
        tenant_id=1,
        display_name="账号B",
        phone_masked="+8612",
        status="在线",
        session_ciphertext="session-b",
    ))
    session.add(TgAccountOnlineState(
        tenant_id=1,
        account_id=12,
        desired_online=True,
        online_status="online",
        last_seen_at=now_value,
        stale_after_at=now_value + timedelta(minutes=10),
    ))
    session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=12, can_send=True))
    first.payload = _normal_payload(1)
    first.claim_owner = "worker-a"
    first.claim_token = "claim-normal"
    second = Action(
        id="action-normal-generation-2",
        tenant_id=1,
        task_id=first.task_id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=12,
        status="executing",
        claim_owner="worker-a",
        claim_token="claim-normal",
        scheduled_at=now_value,
        payload=_normal_payload(2),
    )
    coverages = [_normal_coverage(index, account_id, now_value) for index, account_id in enumerate((11, 12), 1)]
    first.payload = {**first.payload, "coverage_ledger_id": coverages[0].id}
    second.payload = {**second.payload, "coverage_ledger_id": coverages[1].id}
    session.add_all([second, *coverages])
    session.commit()
    return [first, second], coverages


def _normal_payload(index: int) -> dict:
    return {
        "group_id": 7,
        "target_display": "运营群",
        "message_text": "",
        "review_approved": True,
        "cycle_id": "cycle-normal",
        "slot_id": f"cycle-normal:turn:{index}",
        "turn_index": index,
        "ai_generation_id": "cycle-normal",
        "ai_generation_status": "pending",
        "ai_generation_claim_owner": "worker-a",
        "ai_generation_claim_token": "claim-normal",
        "ai_generation_history": "真人用户: 今天按原计划吗？",
    }


def _normal_coverage(index: int, account_id: int, now_value) -> TaskAccountDailyCoverage:
    return TaskAccountDailyCoverage(
        id=f"coverage-normal-{index}",
        tenant_id=1,
        task_id="task-reply-generation",
        group_id=7,
        account_id=account_id,
        coverage_date=now_value.date(),
        state="reserved",
        reserved_action_id="action-reply-generation" if index == 1 else "action-normal-generation-2",
        targeted_at=now_value,
    )


def _invalidate_reply_target(session: Session, action: Action, invalidation: str, now_value) -> None:
    if invalidation == "local_missing":
        target = session.scalar(select(GroupContextMessage).where(
            GroupContextMessage.remote_message_id == "9001",
        ))
        session.delete(target)
    elif invalidation == "stale":
        action.created_at = now_value - timedelta(minutes=10)
    elif invalidation == "permission":
        link = session.scalar(select(TgGroupAccount).where(
            TgGroupAccount.group_id == 7,
            TgGroupAccount.account_id == 11,
        ))
        link.can_send = False
    session.commit()


def _invalid_reply_dependencies(
    session: Session,
    invalidation: str,
) -> GenerationDependencies:
    if invalidation == "remote_missing":
        return _generation_dependencies(
            reply_target_probe=_reply_probe(session),
            reply_messages_fetcher=lambda *_args, **_kwargs: [],
        )
    return _generation_dependencies()


def _generation_dependencies(
    *,
    normal_generator=None,
    reply_generator=None,
    reply_target_probe=None,
    reply_messages_fetcher=None,
) -> GenerationDependencies:
    return GenerationDependencies(
        normal_generator=normal_generator or _forbidden_external,
        reply_generator=reply_generator or _forbidden_external,
        reply_target_probe=reply_target_probe or _forbidden_external,
        reply_messages_fetcher=reply_messages_fetcher or _forbidden_external,
    )


def _reply_payload(context_id: int) -> dict:
    return {
        "group_id": 7,
        "target_display": "运营群",
        "message_text": "",
        "review_approved": True,
        "cycle_id": "cycle-reply",
        "slot_id": "cycle-reply:turn:1",
        "turn_index": 1,
        "reply_to_message_id": 9001,
        "reply_target_author": "真人用户",
        "reply_target_preview": "今天按原计划吗？",
        "reply_target_source": "human_context",
        "context_snapshot_message_id": context_id,
        "context_message_ids": [context_id],
        "ai_generation_id": "cycle-reply",
        "ai_generation_status": "pending",
        "ai_generation_history": "真人用户: 今天按原计划吗？",
    }


def _forbidden_normal_generation(*_args, **_kwargs):
    raise AssertionError("reply action must not use normal generation")


def _forbidden_external(*_args, **_kwargs):
    raise AssertionError("invalid reply target must not call external AI or Telegram")


def _reply_generator(observed: dict[str, object]):
    def generate(session, _tenant_id, config, *, reply_targets, target_label, history):
        observed["provider_transaction"] = session.in_transaction()
        observed["reply_target"] = reply_targets[0]["message_id"]
        assert target_label == "运营群"
        assert "今天按原计划吗" in history
        return [GeneratedContent(
            "就按这个节奏来",
            slot_id=config["generation_slots"][0]["slot_id"],
            sequence_index=1,
            reply_to_sequence_index=1,
        )], 9

    return generate


def _reply_sender(session: Session, observed: dict[str, object]):
    def send(_account_id, _group_id, _content, *_args, **kwargs):
        observed["gateway_transaction"] = session.in_transaction()
        observed["sent_reply_target"] = kwargs["reply_to_message_id"]
        return SendResult(True, remote_message_id="tg-reply-1")

    return send


def _reply_probe(session: Session):
    def probe(*_args, **_kwargs):
        assert session.in_transaction() is False
        return OperationResult(True, detail="可引用")

    return probe


def _reply_fetch(session: Session):
    def fetch(*_args, **_kwargs):
        assert session.in_transaction() is False
        return [SimpleNamespace(remote_message_id="9001")]

    return fetch
