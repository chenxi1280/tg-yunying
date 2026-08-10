from __future__ import annotations

from datetime import datetime

import pytest

from app.models import ContentMixCycle, ContentMixCycleSlot
from app.services.task_center.executors.group_ai_chat import (
    FrozenContentMix,
    SlotSnapshot,
    _content_variation_identity,
    _with_content_variation_key,
    _with_frozen_content_mix,
)
from app.services.task_center.payloads import SendMessagePayload


pytestmark = pytest.mark.no_postgres


def _payload(**updates) -> SendMessagePayload:
    values = {
        "group_id": 7,
        "ai_generation_status": "pending",
        "coverage_ledger_id": "coverage-1",
        "target_reference_revision": 3,
        "coverage_window_date": "2026-08-10",
        "cycle_id": "cycle-a",
        "slot_id": "cycle-a:turn:1",
        "act_type": "natural_chat",
        "relation_kind": "direct",
        "topic_direction": {"title": "今日话题"},
        "teacher_target": {"name": "老师A"},
        "context_message_ids": [101, 102],
    }
    values.update(updates)
    return SendMessagePayload(**values)


def test_variation_identity_ignores_technical_cycle_and_action_slot() -> None:
    first = _content_variation_identity(_payload(), account_id=11)
    rebuilt = _content_variation_identity(
        _payload(cycle_id="cycle-b", slot_id="cycle-b:turn:9"),
        account_id=11,
    )

    assert rebuilt == first


def test_variation_identity_changes_only_when_business_basis_changes() -> None:
    original = _content_variation_identity(_payload(), account_id=11)
    new_context = _content_variation_identity(
        _payload(context_message_ids=[101, 103]),
        account_id=11,
    )
    new_relation = _content_variation_identity(
        _payload(relation_kind="reply", reply_to_message_id=9001),
        account_id=11,
    )
    new_quantity_unit = _content_variation_identity(
        _payload(primary_quantity_slot_id="quantity-slot-2"),
        account_id=11,
    )

    assert new_context != original
    assert new_relation != original
    assert new_quantity_unit != original


def test_variation_identity_is_computed_after_quantity_slot_freeze() -> None:
    raw = SlotSnapshot(11, datetime(2026, 8, 10, 10, 0), _payload())
    first = _finalized_quantity_snapshot(raw, "quantity-slot-1")
    second = _finalized_quantity_snapshot(raw, "quantity-slot-2")

    assert raw.payload.content_variation_key == ""
    assert first.payload.primary_quantity_slot_id == "quantity-slot-1"
    assert second.payload.primary_quantity_slot_id == "quantity-slot-2"
    assert first.payload.content_variation_key != second.payload.content_variation_key


def _finalized_quantity_snapshot(
    raw: SlotSnapshot,
    quantity_slot_id: str,
) -> SlotSnapshot:
    cycle = ContentMixCycle(id="cycle-db", config_revision=7)
    cycle_slot = ContentMixCycleSlot(
        id=f"cycle-slot-{quantity_slot_id}",
        cycle_id=cycle.id,
        slot_index=1,
        primary_quantity_slot_id=quantity_slot_id,
        relation_kind="direct",
    )
    frozen = FrozenContentMix(cycle, {"quality-slot": cycle_slot})
    with_mix = _with_frozen_content_mix(
        raw,
        {"slot_id": "quality-slot"},
        frozen,
    )
    return _with_content_variation_key(with_mix)
