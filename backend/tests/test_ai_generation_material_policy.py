from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    Material,
    Task,
    TaskAccountDailyCoverage,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services.task_center.ai_generation_quality import store_generation_quality
from app.services.task_center.payloads import SendMessagePayload


pytestmark = pytest.mark.no_postgres


def test_phase_c_selects_material_from_generated_intent() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        action, payload = _seed_material_action(session, fallback="text_only")
        data = _generated_data(payload, material_intent="表情包:围观")

        assert store_generation_quality(session, action, payload, data=data) is True

        assert data["media_segments"][0]["material_id"] == 9301
        assert data["media_segments"][0]["source"] == "tg-cache://cache-peer/9301"
        assert data["rule_trace"]["material_intent"] == "表情包:围观"
        assert data["rule_trace"]["material_matched_tags"] == ["围观"]
        assert data["rule_trace"]["material_candidate_count"] == 1


def test_phase_c_material_skip_fails_slot_and_releases_coverage() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        action, payload = _seed_material_action(session, fallback="skip")
        coverage = TaskAccountDailyCoverage(
            id="material-coverage",
            tenant_id=1,
            task_id=action.task_id,
            group_id=201,
            account_id=101,
            coverage_date=date.today(),
            target_count=1,
            state="reserved",
            reserved_action_id=action.id,
        )
        session.add(coverage)
        payload.coverage_ledger_id = coverage.id
        action.payload = payload.model_dump(mode="json")
        data = _generated_data(payload, material_intent="表情包:欢迎")

        assert store_generation_quality(session, action, payload, data=data) is False

        session.refresh(coverage)
        assert action.status == "failed"
        assert action.result["error_code"] == "material_unavailable"
        assert action.payload["rule_trace"]["material_failure_reason"] == "cache_not_ready"
        assert coverage.state == "ready"
        assert coverage.reserved_action_id is None


def _seed_material_action(
    session: Session, *, fallback: str,
) -> tuple[Action, SendMessagePayload]:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(TgAccount(id=101, tenant_id=1, display_name="AI号", phone_masked="101"))
    session.add(
        TgGroup(
            id=201,
            tenant_id=1,
            tg_peer_id="-100201",
            title="素材群",
            auth_status="已授权运营",
            can_send=True,
        ),
    )
    session.add(
        Material(
            id=9301,
            tenant_id=1,
            title="围观表情",
            material_type="表情包",
            content="https://trusted.example.com/watch.webp",
            tags="围观,表情包",
            emoji_asset_kind="image_meme",
            cache_ready_status="ready",
            tg_cache_peer_id="cache-peer",
            tg_cache_message_id="9301",
            asset_fingerprint="fp-9301",
        ),
    )
    task = Task(id="material-task", tenant_id=1, name="素材任务", type="group_ai_chat")
    action = Action(
        id="material-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        account_id=101,
        status="executing",
    )
    session.add_all([task, action])
    session.flush()
    policy = {
        "enabled": True,
        "material_type": "表情包",
        "intent_tag_map": {"表情包:围观": ["围观"], "表情包:欢迎": ["欢迎"]},
        "fallback": fallback,
    }
    payload = SendMessagePayload(
        group_id=201,
        review_approved=True,
        ai_generation_status="pending",
        cycle_id="material-cycle",
        turn_index=1,
        rule_trace={"material_policy": policy},
    )
    action.payload = payload.model_dump(mode="json")
    return action, payload


def _generated_data(payload: SendMessagePayload, *, material_intent: str) -> dict:
    return {
        **payload.model_dump(mode="json"),
        "message_text": "这个先蹲一下",
        "ai_generation_status": "ready",
        "material_intent": material_intent,
        "allow_material": True,
    }
