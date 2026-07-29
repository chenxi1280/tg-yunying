from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import AuditLog, BotProtocolSample, Task, Tenant
from app.search_join_protocol import (
    PURE_CLICK_JISOU_PROFILE_VERSION,
    pure_click_protocol_profile_is_approved,
    upgraded_legacy_pure_click_profile,
)
from scripts import takeover_all_task_fulfillment as takeover_script


pytestmark = pytest.mark.no_postgres


def test_structural_blocker_pauses_only_invalid_task(
    monkeypatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as session:
        session.add(Tenant(id=1, name="单用户"))
        session.add(
            Task(
                id="invalid-legacy-search",
                tenant_id=1,
                name="缺少纯点击字段",
                type="search_join_group",
                status="running",
                type_config={},
            )
        )
        session.commit()
    monkeypatch.setattr(takeover_script, "SessionLocal", session_factory)

    preview = takeover_script.run_takeover(apply=False)
    applied = takeover_script.run_takeover(apply=True)

    assert preview["failures"] == []
    assert preview["blockers"][0]["persisted"] is False
    assert applied["failures"] == []
    assert applied["blockers"][0]["persisted"] is True
    with session_factory() as session:
        task = session.get(Task, "invalid-legacy-search")
        assert task is not None
        assert task.status == "paused"
        assert task.next_run_at is None
        assert task.stats["fulfillment_takeover_blocker_code"] == (
            "task_contract_invalid"
        )


def test_unexpected_value_error_fails_takeover(
    monkeypatch,
) -> None:
    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, *_args):
            return object()

        def rollback(self):
            return None

    monkeypatch.setattr(
        takeover_script,
        "_task_ids",
        lambda _tenant_id: ["unexpected"],
    )
    monkeypatch.setattr(
        takeover_script,
        "_migrate_pure_click_protocol_samples",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        takeover_script,
        "SessionLocal",
        FakeSession,
    )
    monkeypatch.setattr(
        takeover_script,
        "takeover_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bug")),
    )

    result = takeover_script.run_takeover(apply=True)

    assert result["blockers"] == []
    assert result["failures"] == [
        {"task_id": "unexpected", "error": "ValueError:bug"}
    ]


def test_takeover_versions_legacy_jisou_sample_for_pure_click(
    monkeypatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as session:
        session.add(Tenant(id=1, name="单用户"))
        session.add(BotProtocolSample(
            id="legacy-jisou",
            tenant_id=1,
            bot_username="jisou",
            sample_type="search_results",
            sample_purpose="search_join",
            sample_hash="legacy",
            schema_version="jisou-v2-2026-07-28",
            structure_json=_legacy_jisou_profile(),
            pii_scrubbed=True,
            is_active=True,
        ))
        session.commit()
    monkeypatch.setattr(takeover_script, "SessionLocal", session_factory)

    preview = takeover_script.run_takeover(apply=False)
    with session_factory() as session:
        assert session.get(BotProtocolSample, "legacy-jisou").is_active is True
    applied = takeover_script.run_takeover(apply=True)

    assert preview["protocol_samples"]["changed"] == 1
    assert applied["protocol_samples"]["changed"] == 1
    with session_factory() as session:
        old = session.get(BotProtocolSample, "legacy-jisou")
        active = session.scalar(
            select(BotProtocolSample).where(
                BotProtocolSample.is_active.is_(True),
            )
        )
        audits = list(session.scalars(select(AuditLog)))
        assert old.is_active is False
        assert active.schema_version == PURE_CLICK_JISOU_PROFILE_VERSION
        assert pure_click_protocol_profile_is_approved(active.structure_json)
        assert len(audits) == 1


@pytest.mark.parametrize("case", [
    ("other-version", ["join_candidate"], []),
    ("jisou-v2-2026-07-28", ["join_candidate", "external"], []),
    ("jisou-v2-2026-07-28", ["join_candidate"], ["join"]),
])
def test_legacy_jisou_upgrade_rejects_non_exact_samples(case) -> None:
    schema_version, effects, membership_effects = case
    profile = _legacy_jisou_profile()
    result_page = profile["page_fingerprints"][-1]
    result_page["button_effects_any"] = effects
    result_page["membership_side_effects_allowed"] = membership_effects

    assert upgraded_legacy_pure_click_profile(
        profile,
        schema_version=schema_version,
    ) is None


def test_pure_click_protocol_requires_target_open_effect() -> None:
    profile = _legacy_jisou_profile()
    result_page = profile["page_fingerprints"][-1]
    result_page["button_effects_any"] = ["navigate_only"]
    result_page["membership_side_effects_allowed"] = ["none"]

    assert pure_click_protocol_profile_is_approved(profile) is False


def _legacy_jisou_profile() -> dict:
    return {
        "page_fingerprints": [
            {
                "page_phase": "verification_page",
                "text_enums": ["human_verification"],
            },
            {"page_phase": "hot_list_page", "text_enums": ["hot_list"]},
            {
                "page_phase": "search_category_page",
                "button_text_enums_any": [
                    "jisou_group_category",
                    "jisou_channel_category",
                ],
                "selector_rules": [{
                    "row": 0,
                    "col": 0,
                    "button_type": "callback_data",
                    "effect": "unknown",
                    "normalized_text": "jisou_group_category",
                }],
            },
            {
                "page_phase": "group_result_page",
                "button_effects_any": ["join_candidate", "navigate_only"],
            },
        ],
    }
