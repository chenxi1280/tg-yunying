from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram.contracts import GroupMessageSnapshot
from app.models import GroupContextMessage, OperationTarget, Tenant, TenantLearningProfile, TenantLearningQualityRule, TenantLearningRun, TenantLearningSample, TenantLearningSource, TgAccount, TgGroup, TgGroupAccount
from app.services.group_listeners import collect_group_context
from app.services.tenant_learning_samples import record_group_learning_sample
from app.services.tenant_target_profile import (
    clear_profile,
    get_target_profile_overview,
    list_source_candidates,
    rebuild_profile,
    start_source_run,
    update_sample_status,
    update_quality_rules,
)
from app.services.tenant_target_profile import tenant_learning_profile_preview
from app.services.tenant_target_profile_admin import list_profile_versions, restore_profile_version, update_profile_settings


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_target_profile_overview_creates_single_empty_tenant_profile() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id="-10031", title="旧群"))
        session.commit()

        overview = get_target_profile_overview(session, 1)
        second = get_target_profile_overview(session, 1)
        profiles = list(session.scalars(select(TenantLearningProfile)))

    assert overview["profile_version"] == 0
    assert overview["status"] == "sample_insufficient"
    assert overview["usage_scope"] == ["group_ai_chat", "channel_comment", "discussion_reply"]
    assert second["profile_id"] == overview["profile_id"]
    assert len(profiles) == 1
    assert profiles[0].tenant_id == 1


def test_source_candidates_explain_recommendation_and_auto_sync_blockers() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id="-10031", title="活群"),
                OperationTarget(id=32, tenant_id=1, target_type="channel", tg_peer_id="-10032", title="频道"),
                TgGroup(id=41, tenant_id=1, tg_peer_id="-10031", title="活群", listener_enabled=True),
            ]
        )
        session.commit()

        result = list_source_candidates(session, 1)

    items = {item["target_id"]: item for item in result["items"]}
    assert items[31]["recommended"] is True
    assert items[31]["recommend_reason"] == "可监听目标"
    assert items[31]["cannot_auto_sync_reason"] == ""
    assert items[32]["recommended"] is False
    assert items[32]["cannot_auto_sync_reason"] == "target_not_listenable"


def test_quality_rule_update_requires_reason_and_records_recompute_run() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        with pytest.raises(ValueError, match="请填写质量规则变更原因"):
            update_quality_rules(session, 1, {"text_filters": {"keywords": ["广告"]}}, actor="tester", reason="")

        payload = update_quality_rules(
            session,
            1,
            {"text_filters": {"keywords": ["广告"]}, "scoring_thresholds": {"accepted": 80}},
            actor="tester",
            reason="过滤广告样本",
        )
        session.commit()
        rule = session.scalar(select(TenantLearningQualityRule))
        run = session.scalar(select(TenantLearningRun).where(TenantLearningRun.run_type == "recompute_candidates"))

    assert payload["rule_version"] == 1
    assert rule is not None
    assert rule.text_filters == {"keywords": ["广告"]}
    assert run is not None
    assert run.status == "success"
    assert run.quality_rule_version == 1


def test_rebuild_profile_uses_accepted_samples_and_records_version_run() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id="-10031", title="活群"))
        source = TenantLearningSource(tenant_id=1, target_id=31, source_kind="group")
        session.add(source)
        session.flush()
        session.add_all(
            [
                TenantLearningSample(tenant_id=1, source_id=source.id, source_message_id="m1", text="这个活动几点开始？", learning_status="accepted", quality_score=95),
                TenantLearningSample(tenant_id=1, source_id=source.id, source_message_id="m2", text="广告模板", learning_status="rejected", quality_score=0),
            ]
        )
        session.commit()

        rebuilt = rebuild_profile(session, 1, actor="tester", reason="生成全站画像")
        session.commit()

        assert rebuilt["profile_version"] == 1
        assert rebuilt["source_sample_count"] == 1
        assert "这个活动几点开始" in rebuilt["style_summary"]
        run = session.scalar(select(TenantLearningRun).where(TenantLearningRun.run_type == "rebuild"))
        assert run is not None
        assert run.status == "success"
        assert run.profile_version == 1


def test_sample_status_update_and_clear_profile_require_reasons() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id="-10031", title="活群"))
        source = TenantLearningSource(tenant_id=1, target_id=31, source_kind="group")
        session.add(source)
        session.flush()
        sample = TenantLearningSample(tenant_id=1, source_id=source.id, source_message_id="m1", text="像真人的句子", learning_status="candidate")
        session.add(sample)
        session.commit()

        with pytest.raises(ValueError, match="请填写样本调整原因"):
            update_sample_status(session, 1, sample.id, "accepted", actor="tester", reason="")

        updated = update_sample_status(session, 1, sample.id, "accepted", actor="tester", reason="可学习")
        rebuilt = rebuild_profile(session, 1, actor="tester", reason="重建后再清空")
        cleared = clear_profile(session, 1, actor="tester", reason="重新学习")
        session.commit()

    assert updated["learning_status"] == "accepted"
    assert rebuilt["profile_version"] == 1
    assert cleared["profile_version"] == 2
    assert cleared["source_sample_count"] == 0
    assert cleared["status"] == "sample_insufficient"


def test_source_sync_and_history_pull_write_visible_runs() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id="-10031", title="活群"))
        session.add(TgGroup(id=41, tenant_id=1, tg_peer_id="-10031", title="活群", listener_enabled=True))
        session.add(TgAccount(id=51, tenant_id=1, phone_masked="+10000000001", display_name="监听号", status="在线"))
        session.add(TgGroupAccount(id=61, tenant_id=1, group_id=41, account_id=51, is_listener=True))
        source = TenantLearningSource(tenant_id=1, target_id=31, source_kind="group")
        session.add(source)
        session.add(GroupContextMessage(tenant_id=1, group_id=41, listener_account_id=51, sender_name="真人用户", content="这个活动几点开始", remote_message_id="m1"))
        session.commit()
        source_id = source.id

        sync_run = start_source_run(session, 1, source_id, "sync", actor="tester")
        pull_run = start_source_run(session, 1, source_id, "pull_history", actor="tester")
        sample = session.scalar(select(TenantLearningSample).where(TenantLearningSample.source_message_id == "m1"))
        sample_status = sample.learning_status if sample else ""
        sample_rule_version = sample.quality_rule_version if sample else 0
        session.commit()

    assert sync_run["run_type"] == "sync"
    assert pull_run["run_type"] == "pull_history"
    assert sync_run["status"] == "success"
    assert sync_run["sample_count"] == 1
    assert pull_run["source_id"] == source_id
    assert sample is not None
    assert sample_status == "accepted"
    assert sample_rule_version == 1


def test_sampling_requires_explicit_enabled_learning_source() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(id=41, tenant_id=1, tg_peer_id="-10031", title="活群", listener_enabled=True)
        session.add_all([
            group,
            OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id="-10031", title="活群"),
        ])
        session.commit()

        sample = record_group_learning_sample(
            session,
            group,
            SimpleNamespace(remote_message_id="m1", content="没有配置来源时不能学习", sender_name="真人用户"),
        )
        source_count = session.query(TenantLearningSource).count()
        sample_count = session.query(TenantLearningSample).count()
        session.commit()

    assert sample is None
    assert source_count == 0
    assert sample_count == 0


def test_group_listener_ignored_sender_is_not_recorded_as_learning_sample(monkeypatch) -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(id=41, tenant_id=1, tg_peer_id="-10031", title="活群", listener_enabled=True)
        session.add_all([
            group,
            OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id="-10031", title="活群"),
            TgAccount(id=51, tenant_id=1, phone_masked="+10000000001", display_name="监听号", status="在线"),
            TgAccount(id=52, tenant_id=1, phone_masked="+10000000002", display_name="托管号", status="在线"),
            TgGroupAccount(id=61, tenant_id=1, group_id=41, account_id=51, is_listener=True),
            TgGroupAccount(id=62, tenant_id=1, group_id=41, account_id=52, is_listener=False),
            TenantLearningSource(tenant_id=1, target_id=31, source_kind="group"),
        ])
        session.commit()
        snapshots = [
            GroupMessageSnapshot(
                remote_message_id="managed-m1",
                sender_peer_id="account:52",
                sender_name="托管号",
                content="托管账号自己的消息不能学习",
            )
        ]
        monkeypatch.setattr("app.services.group_listeners.credentials_for_account", lambda *_args, **_kwargs: {})
        monkeypatch.setattr("app.services.group_listeners.gateway.fetch_group_messages", lambda *args, **kwargs: snapshots)

        inserted = collect_group_context(session, group, [51])
        sample_count = session.scalar(select(TenantLearningSample).where(TenantLearningSample.source_message_id == "managed-m1"))
        context_count = session.scalar(select(GroupContextMessage).where(GroupContextMessage.remote_message_id == "managed-m1"))
        session.commit()

    assert inserted == 0
    assert sample_count is None
    assert context_count is None


def test_quality_rule_update_recomputes_candidates_and_preserves_manual_decisions() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id="-10031", title="活群"))
        source = TenantLearningSource(tenant_id=1, target_id=31, source_kind="group")
        session.add(source)
        session.flush()
        session.add_all([
            TenantLearningSample(tenant_id=1, source_id=source.id, source_message_id="auto", text="广告文案", learning_status="accepted"),
            TenantLearningSample(tenant_id=1, source_id=source.id, source_message_id="manual", text="广告文案人工采纳", learning_status="accepted", decision_by="tester"),
            TenantLearningSample(tenant_id=1, source_id=source.id, source_message_id="bot", text="正常聊天", learning_status="accepted", is_bot=True),
        ])
        session.commit()

        update_quality_rules(
            session,
            1,
            {"forbidden_patterns": {"keywords": ["广告"], "links": True, "contacts": True}},
            actor="tester",
            reason="过滤广告",
        )
        statuses = {
            sample.source_message_id: sample.learning_status
            for sample in session.scalars(select(TenantLearningSample)).all()
        }
        reasons = {
            sample.source_message_id: sample.reject_reason
            for sample in session.scalars(select(TenantLearningSample)).all()
        }
        run = session.scalar(select(TenantLearningRun).where(TenantLearningRun.run_type == "recompute_candidates").order_by(TenantLearningRun.created_at.desc()))
        run_sample_count = run.sample_count if run else 0
        run_rejected_count = run.rejected_count if run else 0
        session.commit()

    assert statuses == {"auto": "rejected", "manual": "accepted", "bot": "rejected"}
    assert reasons["auto"] == "forbidden_keyword"
    assert reasons["manual"] == ""
    assert reasons["bot"] == "bot_sender"
    assert run is not None
    assert run_sample_count == 2
    assert run_rejected_count == 2


def test_profile_preview_is_tenant_level_for_all_runtime_scenes() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TenantLearningProfile(tenant_id=1, profile_version=3, status="active", style_summary="真人短句口吻", source_sample_count=8))
        session.commit()

        group_preview = tenant_learning_profile_preview(session, 1, "group_chat")
        comment_preview = tenant_learning_profile_preview(session, 1, "channel_comment")

    assert group_preview["profile_hit_summary"] == "真人短句口吻"
    assert comment_preview["profile_hit_summary"] == "真人短句口吻"
    assert group_preview["profile_id"] == comment_preview["profile_id"]
    assert group_preview["profile_scene"] == "group_chat"
    assert comment_preview["profile_scene"] == "channel_comment"


def test_profile_versions_restore_and_settings_are_tenant_level() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id="-10031", title="活群"))
        source = TenantLearningSource(tenant_id=1, target_id=31, source_kind="group")
        session.add(source)
        session.flush()
        session.add(TenantLearningSample(tenant_id=1, source_id=source.id, source_message_id="m1", text="像真人的句子", learning_status="accepted"))
        session.commit()

        rebuilt = rebuild_profile(session, 1, actor="tester", reason="生成画像")
        version_id = list_profile_versions(session, 1)["items"][0]["id"]
        profile = session.scalar(select(TenantLearningProfile).where(TenantLearningProfile.tenant_id == 1))
        assert profile is not None
        profile.style_summary = "被改坏"
        restored = restore_profile_version(session, 1, version_id, actor="tester", reason="恢复画像")
        disabled = update_profile_settings(session, 1, {"learning_enabled": False}, actor="tester", reason="暂停学习")
        session.commit()

    assert rebuilt["profile_version"] == 1
    assert restored["profile_version"] == 2
    assert restored["style_summary"] == "像真人的句子"
    assert disabled["learning_enabled"] is False
