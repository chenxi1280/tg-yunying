from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import (
    Action,
    AiContentScopeTakeoverBatch,
    ContentMixContract,
    ContentMixCycle,
    ContentMixCycleSlot,
    ExecutionAttempt,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.services._common import _now
from app.services.task_center.ai_content_scope_takeover import (
    preview_ai_content_scope_takeover,
)
from app.services.task_center.payloads import SendMessagePayload


def sessions():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def seed_scope(session: Session) -> None:
    session.add(Tenant(id=1, name="tenant"))
    session.add(TgGroup(id=8, tenant_id=1, tg_peer_id="-1008", title="group"))
    session.add(TgAccount(
        id=11,
        tenant_id=1,
        display_name="account",
        phone_masked="***11",
        status="在线",
        session_ciphertext="session",
    ))
    session.add(TgGroupAccount(
        tenant_id=1,
        group_id=8,
        account_id=11,
        can_send=True,
    ))
    session.add(Task(
        id="task-ai",
        tenant_id=1,
        name="AI",
        type="group_ai_chat",
        status="running",
        type_config={"target_group_id": 8},
    ))
    session.flush()


def seed_bound_legacy_action(
    session: Session,
    action_id: str,
    **payload_updates,
) -> Action:
    identifiers = _content_identifiers(action_id)
    start = datetime(2026, 7, 31, 16, tzinfo=UTC)
    _seed_content_ledger(session, identifiers, start)
    action = Action(
        id=action_id,
        tenant_id=1,
        task_id="task-ai",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        scheduled_at=_now() - timedelta(minutes=1),
        status="pending",
        primary_quantity_slot_id=identifiers[1],
        content_mix_cycle_slot_id=identifiers[3],
        content_mix_slot_attempt=1,
        payload=_legacy_payload(
            identifiers[1], identifiers[3], **payload_updates,
        ),
    )
    session.add(action)
    session.add(ContentMixCycleSlot(
        id=identifiers[3],
        tenant_id=1,
        cycle_id=identifiers[2],
        slot_index=1,
        primary_quantity_slot_id=identifiers[1],
        relation_kind="direct",
        current_action_id=action_id,
        slot_state="materialized",
    ))
    session.flush()
    return action


def _content_identifiers(action_id: str) -> tuple[str, str, str, str]:
    return (
        f"ledger-{action_id}",
        f"quantity-{action_id}",
        f"cycle-{action_id}",
        f"cycle-slot-{action_id}",
    )


def _seed_content_ledger(
    session: Session,
    identifiers: tuple[str, str, str, str],
    start: datetime,
) -> None:
    ledger_id, quantity_id, cycle_id, _ = identifiers
    session.add(TaskDayLedger(
        id=ledger_id,
        tenant_id=1,
        task_id="task-ai",
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 8, 1),
        period_start_at=start,
        deadline_at=start + timedelta(days=1),
        day_phase="full_day_committed",
        planning_anchor_at=start,
    ))
    session.add(TaskGroupDailyMessageSlot(
        id=quantity_id,
        tenant_id=1,
        task_id="task-ai",
        task_day_ledger_id=ledger_id,
        target_operation_target_id=8,
        slot_kind="extra_volume",
        slot_ordinal=1,
    ))
    _seed_content_cycle(session, identifiers)


def _seed_content_cycle(
    session: Session,
    identifiers: tuple[str, str, str, str],
) -> None:
    ledger_id, _, cycle_id, _ = identifiers
    session.add(ContentMixCycle(
        id=cycle_id,
        tenant_id=1,
        task_id="task-ai",
        target_operation_target_id=8,
        task_day_ledger_id=ledger_id,
        cycle_seq=1,
        config_revision=1,
        scope_total_slots=1,
        allocation_seed="seed",
        allocation_closed_at=_now(),
    ))
    session.add(ContentMixContract(
        id=f"contract-{cycle_id}",
        tenant_id=1,
        content_mix_scope_key=f"ai:task-ai:8:{cycle_id}:1",
        content_contract_version=1,
        scope_total_slots=1,
        allocation_seed="seed",
        direct_planned_count=1,
    ))
    session.flush()


def _legacy_payload(quantity_id: str, cycle_slot_id: str, **updates) -> dict:
    values = {
        "chat_id": "-1008",
        "group_id": 8,
        "message_text": "legacy body",
        "chat_mode": "idle_warmup",
        "primary_quantity_slot_id": quantity_id,
        "content_mix_cycle_slot_id": cycle_slot_id,
        "relation_kind": "direct",
    }
    values.update(updates)
    return SendMessagePayload.model_validate(values).model_dump(mode="json")


def seed_terminal_action(session: Session, action_id: str) -> None:
    session.add(Action(
        id=action_id,
        tenant_id=1,
        task_id="task-ai",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        scheduled_at=_now(),
        status="success",
        payload={"chat_id": "-1008", "group_id": 8, "message_text": "sent"},
        result={"success": True, "remote_message_id": "remote-terminal"},
    ))


def seed_unknown_action(session: Session, action_id: str) -> None:
    action = Action(
        id=action_id,
        tenant_id=1,
        task_id="task-ai",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        scheduled_at=_now(),
        status="unknown_after_send",
        payload={"chat_id": "-1008", "group_id": 8, "message_text": "unknown"},
        result={"error_code": "unknown_after_send"},
    )
    session.add(action)
    session.add(ExecutionAttempt(
        id=f"attempt-{action_id}",
        tenant_id=1,
        action_id=action_id,
        account_id=11,
        attempt_no=1,
        status="result_unknown",
        before_call_at=_now() - timedelta(seconds=2),
        gateway_call_started_at=_now() - timedelta(seconds=1),
        after_call_at=_now(),
        failure_type="unknown_after_send",
    ))


def preview(
    session: Session,
    *,
    supersedes_batch_id: str | None = None,
) -> AiContentScopeTakeoverBatch:
    return preview_ai_content_scope_takeover(
        session,
        cutoff_at=_now() + timedelta(minutes=1),
        actor="release-owner",
        dispatcher_scope="task_center_dispatch",
        release_version="release-test",
        config_version="dispatch-rebuild-v3",
        supersedes_batch_id=supersedes_batch_id,
    )
