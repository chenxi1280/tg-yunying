from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.integrations.telegram import SendResult
from app.models import (
    Action,
    Task,
    TaskAccountDailyCoverage,
    Tenant,
    TgAccount,
    TgAccountOnlineState,
    TgGroup,
    TgGroupAccount,
)
from app.services.task_center import dispatcher
from app.services.task_center import payloads as task_payloads
from app.services.task_center.ai_generation_dependencies import GenerationDependencies
from app.services.task_center.ai_generator import GeneratedContent
from app.services.task_center.ai_message_memory import reserve_group_ai_message


def seed_pending_generation_scope(session, now_value, claim_limit: int) -> None:
    _add_pending_base_rows(session, now_value)
    session.add(Task(
        id="task-hard-hourly-ai",
        tenant_id=1,
        name="硬目标",
        type="group_ai_chat",
        status="running",
        priority=1,
        type_config={
            "target_group_id": 7,
            "ai_model": "mino-v2.5",
            "topic_directions": [{"title": "日常活跃", "weight": 1}],
        },
    ))
    session.add(_hard_hourly_action(
        "action-hard-hourly-ai",
        11,
        now_value,
        turn_index=1,
        account_role="活跃群友",
    ))
    session.add(_hard_hourly_action(
        "action-hard-hourly-ai-sibling",
        12,
        now_value if claim_limit == 2 else now_value + timedelta(seconds=10),
        turn_index=2,
        account_role="追问群友",
    ))
    session.commit()


def _add_pending_base_rows(session, now_value) -> None:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(TgGroup(
        id=7,
        tenant_id=1,
        tg_peer_id="-1007",
        title="运营群",
        auth_status="已授权运营",
        can_send=True,
        require_review=False,
    ))
    for account_id in (11, 12):
        session.add(TgAccount(
            id=account_id,
            tenant_id=1,
            display_name=f"账号{account_id}",
            phone_masked=f"+861***00{account_id}",
            status="在线",
            session_ciphertext=f"session-{account_id}",
        ))
        session.add(TgAccountOnlineState(
            tenant_id=1,
            account_id=account_id,
            desired_online=True,
            online_status="online",
            last_seen_at=now_value,
            stale_after_at=now_value + timedelta(minutes=10),
        ))
        session.add(TgGroupAccount(
            tenant_id=1,
            group_id=7,
            account_id=account_id,
            can_send=True,
            permission_label="可发言",
        ))


def _hard_hourly_action(action_id, account_id, scheduled_at, *, turn_index, account_role) -> Action:
    cycle_id = "cycle-hard-hourly-ai"
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="task-hard-hourly-ai",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=account_id,
        status="pending",
        scheduled_at=scheduled_at,
        payload={
            "group_id": 7,
            "target_display": "运营群",
            "message_text": "",
            "review_approved": True,
            "hard_hourly_target": True,
            "ai_generation_status": "pending",
            "ai_generation_id": cycle_id,
            "cycle_id": cycle_id,
            "slot_id": f"{cycle_id}:turn:{turn_index}",
            "ai_generation_history": "真人: 今天怎么安排",
            "account_role": account_role,
            "reply_to_message_id": None,
        },
    )


def configure_pending_generation(monkeypatch, generated: dict, sent: dict) -> GenerationDependencies:
    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())

    def fake_generate(_session, _tenant_id, config, *, count, target_label, history):
        assert _session.in_transaction() is False
        generated.update({
            "model": config["ai_model"],
            "count": count,
            "target": target_label,
            "history": history,
            "personas": config["account_personas"],
        })
        texts = ["今天先看看群公告", "第二条我也等等看"][:count]
        return [
            GeneratedContent(text, slot_id=slot["slot_id"], sequence_index=index)
            for index, (slot, text) in enumerate(zip(config["generation_slots"], texts, strict=True), 1)
        ], 17

    def fake_send(account_id, _group_pk, content, *_args, **_kwargs):
        sent.update({"account_id": account_id, "content": content})
        return SendResult(True, remote_message_id="tg-ai-generated")

    monkeypatch.setattr(dispatcher.gateway, "send_message", fake_send)
    return _normal_generation_dependencies(fake_generate)


def _normal_generation_dependencies(generator) -> GenerationDependencies:
    return GenerationDependencies(
        normal_generator=generator,
        reply_generator=lambda *_args, **_kwargs: pytest.fail("normal action must not use reply generator"),
        reply_target_probe=lambda *_args, **_kwargs: pytest.fail("normal action must not probe reply target"),
        reply_messages_fetcher=lambda *_args, **_kwargs: pytest.fail("normal action must not fetch reply messages"),
    )


def assert_claimed_generation_batch(session, claimed, expected_generation_count: int) -> None:
    assert len(claimed) == expected_generation_count
    assert len({row.claim_token for row in claimed}) == 1
    if expected_generation_count != 2:
        return
    payload = task_payloads.SendMessagePayload.model_validate(claimed[0].payload or {})
    batch = dispatcher._ai_generation_dispatch._pending_generation_batch(session, claimed[0], payload)
    assert [row.id for row, _payload in batch] == [
        "action-hard-hourly-ai",
        "action-hard-hourly-ai-sibling",
    ], [
        (row.id, row.status, row.claim_owner, row.claim_token, row.payload)
        for row in session.scalars(select(Action).where(Action.task_id == "task-hard-hourly-ai"))
    ]


def seed_duplicate_generation_scope(session, now_value):
    _add_pending_base_rows(session, now_value)
    task = Task(
        id="task-hard-hourly-ai-dup",
        tenant_id=1,
        name="硬目标重复",
        type="group_ai_chat",
        status="running",
        priority=1,
        type_config={"target_group_id": 7, "topic_directions": [{"title": "日常活跃", "weight": 1}]},
    )
    session.add(task)
    reserve_group_ai_message(
        session,
        tenant_id=1,
        group_id=7,
        task_id="old-ai",
        account_id=99,
        raw_text="今天先看看群公告",
        now=now_value - timedelta(minutes=1),
    )
    action = _duplicate_action(task.id, now_value)
    session.add(action)
    session.flush()
    coverage = _duplicate_coverage(task.id, action.id, now_value)
    session.add(coverage)
    action.payload = {**action.payload, "coverage_ledger_id": coverage.id, "account_coverage_mode": "all_accounts_daily"}
    session.commit()
    return action, coverage


def _duplicate_action(task_id: str, now_value) -> Action:
    cycle_id = "cycle-hard-hourly-ai-dup"
    return Action(
        id="action-hard-hourly-ai-dup",
        tenant_id=1,
        task_id=task_id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status="pending",
        scheduled_at=now_value,
        payload={
            "group_id": 7,
            "target_display": "运营群",
            "message_text": "",
            "review_approved": True,
            "hard_hourly_target": True,
            "ai_generation_status": "pending",
            "ai_generation_id": cycle_id,
            "cycle_id": cycle_id,
            "slot_id": f"{cycle_id}:turn:1",
            "ai_generation_history": "真人: 今天怎么安排",
        },
    )


def _duplicate_coverage(task_id: str, action_id: str, now_value) -> TaskAccountDailyCoverage:
    return TaskAccountDailyCoverage(
        id="coverage-hard-hourly-ai-dup",
        tenant_id=1,
        task_id=task_id,
        group_id=7,
        account_id=11,
        coverage_date=now_value.date(),
        state="reserved",
        reserved_action_id=action_id,
        targeted_at=now_value,
    )


def configure_duplicate_generation(monkeypatch) -> GenerationDependencies:
    monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        dispatcher.gateway,
        "send_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("duplicate must not call TG")),
    )

    def duplicate_generator(_session, _tenant_id, config, **_kwargs):
        return [GeneratedContent(
            "今天先看看群公告",
            slot_id=config["generation_slots"][0]["slot_id"],
            sequence_index=1,
        )], 9

    return _normal_generation_dependencies(duplicate_generator)


def pending_generation_cycle_batch(session, now_value):
    payload = {
        "group_id": 7,
        "message_text": "",
        "ai_generation_status": "pending",
        "ai_generation_id": "cycle-new",
        "cycle_id": "cycle-new",
        "ai_generation_history": "第二条真人上下文",
    }
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(Task(id="task-ai-batch", tenant_id=1, name="ai batch", type="group_ai_chat", status="running"))
    session.add_all(_generation_cycle_actions(now_value, payload))
    session.commit()
    action = session.get(Action, "action-current")
    _mark_generation_claims(session)
    validated = dispatcher.validate_action_payload(action.action_type, action.payload or {})
    return dispatcher._ai_generation_dispatch._pending_generation_batch(session, action, validated)


def _generation_cycle_actions(now_value, payload: dict) -> list[Action]:
    rows = [
        ("action-current", 11, now_value, payload),
        ("action-old-cycle", 12, now_value - timedelta(seconds=1), {
            **payload,
            "ai_generation_id": "cycle-old",
            "cycle_id": "cycle-old",
            "ai_generation_history": "第一条真人上下文",
        }),
        ("action-new-sibling", 13, now_value + timedelta(seconds=1), {**payload, "turn_index": 2}),
        ("action-reply-sibling", 14, now_value, {**payload, "reply_to_message_id": 9001}),
        ("action-other-claim", 15, now_value, payload),
    ]
    return [_cycle_action(*row[:3], payload=row[3]) for row in rows]


def _cycle_action(action_id, account_id, scheduled_at, *, payload) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="task-ai-batch",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=account_id,
        status="pending",
        scheduled_at=scheduled_at,
        payload=payload,
    )


def _mark_generation_claims(session) -> None:
    claimed_ids = ["action-current", "action-old-cycle", "action-new-sibling", "action-reply-sibling"]
    for row in session.scalars(select(Action).where(Action.id.in_(claimed_ids))):
        row.status = "executing"
        row.claim_owner = "worker-a"
        row.claim_token = "claim-cycle"
        row.payload = {
            **row.payload,
            "ai_generation_claim_owner": "worker-a",
            "ai_generation_claim_token": "claim-cycle",
        }
    other = session.get(Action, "action-other-claim")
    other.status = "executing"
    other.payload = {
        **other.payload,
        "ai_generation_claim_owner": "worker-a",
        "ai_generation_claim_token": "claim-other-cycle",
    }
    session.commit()


def assert_quality_retry_states(generated_configs: list[dict], action_states: list[tuple]) -> None:
    assert [config["_ai_fallback_stage"] for config in generated_configs] == [
        "primary_m3",
        "fallback_m25",
        "primary_m3",
    ]
    assert [state[0] for state in action_states] == [11, 12]
    assert [state[2]["slot_id"] for state in action_states] == [
        "task-slot-retry:cycle:1:turn:1",
        "task-slot-retry:cycle:1:turn:2",
    ]
    assert [state[2]["rewrite_attempts"] for state in action_states] == [0, 0]
    assert [state[2]["message_text"] for state in action_states] == [
        "花花老师价格大概多少",
        "主任最近约新妹子了吗",
    ]
    assert [state[2]["ai_generation_status"] for state in action_states] == ["ready", "ready"]
    assert [state[1] for state in action_states] == ["success", "success"]
