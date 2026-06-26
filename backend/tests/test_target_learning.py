from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountStatus, ChannelMessage, ChannelMessageComment, OperationTarget, TargetLearningProfile, TargetLearningSample, Tenant, TgAccount, TgGroup
from app.services.operations import operation_target_detail
from app.services.operations_center_learning import refresh_listener_learning
from app.services.task_center.details import _quality_risks
from app.services.target_learning import CHANNEL_COMMENT_SCENE, clear_learning_profile, record_channel_comment_learning_sample, record_group_learning_sample, update_learning_sample_status
from app.services.target_learning_versions import list_learning_profile_versions, restore_learning_profile_version


def test_channel_comment_learning_builds_profile_and_downweights_ads():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="频道"))
        session.add(TgAccount(id=51, tenant_id=1, display_name="托管评论号", username="managed_comment", phone_masked="51", status=AccountStatus.ACTIVE.value))
        session.add(ChannelMessage(id=41, tenant_id=1, channel_target_id=31, message_id=9101, content_preview="频道消息"))
        session.add_all(
            [
                ChannelMessageComment(
                    tenant_id=1,
                    channel_target_id=31,
                    channel_message_id=41,
                    comment_message_id=8101,
                    author_name="真人用户",
                    content_preview="这个价格区间有人体验过吗？",
                ),
                ChannelMessageComment(
                    tenant_id=1,
                    channel_target_id=31,
                    channel_message_id=41,
                    comment_message_id=8102,
                    author_name="广告号",
                    content_preview="招商推广，精品必吃榜，踩坑包赔",
                ),
                ChannelMessageComment(
                    tenant_id=1,
                    channel_target_id=31,
                    channel_message_id=41,
                    comment_message_id=8103,
                    author_name="评论机器人",
                    content_preview="机器人评论不能学",
                    is_bot=True,
                ),
                ChannelMessageComment(
                    tenant_id=1,
                    channel_target_id=31,
                    channel_message_id=41,
                    comment_message_id=8104,
                    author_username="managed_comment",
                    author_name="托管评论号",
                    content_preview="自己账号的评论不能学",
                ),
            ]
        )
        session.flush()

        for comment in session.scalars(select(ChannelMessageComment).order_by(ChannelMessageComment.comment_message_id.asc())):
            record_channel_comment_learning_sample(session, comment)
        session.commit()

        samples = session.scalars(select(TargetLearningSample).order_by(TargetLearningSample.source_message_id.asc())).all()
        statuses = {sample.source_message_id: sample.learning_status for sample in samples}
        profile = session.scalar(select(TargetLearningProfile).where(TargetLearningProfile.target_id == 31))

    assert statuses == {"8101": "accepted", "8102": "downweighted", "8103": "rejected", "8104": "rejected"}
    assert profile is not None
    assert profile.profile_scene == CHANNEL_COMMENT_SCENE
    assert profile.source_sample_count == 1
    assert profile.profile_version >= 1


def test_channel_comment_learning_reclassifies_existing_bot_sample():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="频道"))
        session.add(ChannelMessage(id=41, tenant_id=1, channel_target_id=31, message_id=9101, content_preview="频道消息"))
        comment = ChannelMessageComment(
            tenant_id=1,
            channel_target_id=31,
            channel_message_id=41,
            comment_message_id=8101,
            author_name="真人用户",
            content_preview="这个价格区间有人体验过吗？",
        )
        session.add(comment)
        session.flush()

        record_channel_comment_learning_sample(session, comment)
        comment.is_bot = True
        record_channel_comment_learning_sample(session, comment)
        session.commit()

        sample = session.scalar(select(TargetLearningSample).where(TargetLearningSample.source_message_id == "8101"))
        profile = session.scalar(select(TargetLearningProfile).where(TargetLearningProfile.target_id == 31))

    assert sample is not None
    assert sample.learning_status == "rejected"
    assert sample.reject_reason == "bot"
    assert profile is not None
    assert profile.source_sample_count == 0


def test_group_learning_duplicate_sample_is_idempotent(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    snapshot = SimpleNamespace(
        remote_message_id="dup-1001",
        sender_peer_id="real-user",
        sender_username="real_user",
        sender_name="真人用户",
        is_bot=False,
        message_type="text",
        content="这个活动几点开始？",
        caption="",
        sent_at=None,
    )

    monkeypatch.setattr("app.services.target_learning._existing_sample", lambda *_args, **_kwargs: None)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="活群", auth_status="已授权运营"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id="-1007", title="活群"))
        session.flush()

        first = record_group_learning_sample(session, session.get(TgGroup, 7), snapshot)
        second = record_group_learning_sample(session, session.get(TgGroup, 7), snapshot)
        session.commit()
        samples = list(session.scalars(select(TargetLearningSample).where(TargetLearningSample.source_message_id == "dup-1001")))

    assert first is not None
    assert second is None
    assert len(samples) == 1


def test_group_learning_rejects_coarse_language_sample():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    snapshot = SimpleNamespace(
        remote_message_id="coarse-1001",
        sender_peer_id="real-user",
        sender_username="real_user",
        sender_name="真人用户",
        is_bot=False,
        message_type="text",
        content="这事真傻逼，别同步这种爆粗内容",
        caption="",
        sent_at=None,
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="活群", auth_status="已授权运营"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="group", tg_peer_id="-1007", title="活群"))
        session.flush()

        sample = record_group_learning_sample(session, session.get(TgGroup, 7), snapshot)
        assert sample is not None
        learning_status = sample.learning_status
        reject_reason = sample.reject_reason
        session.commit()

    assert learning_status == "rejected"
    assert reject_reason == "coarse_language"


def test_learning_profile_version_restore_creates_new_current_version():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="频道"))
        session.add(ChannelMessage(id=41, tenant_id=1, channel_target_id=31, message_id=9101, content_preview="频道消息"))
        comment = ChannelMessageComment(
            tenant_id=1,
            channel_target_id=31,
            channel_message_id=41,
            comment_message_id=8101,
            author_name="真人用户",
            content_preview="这个价格区间有人体验过吗？",
        )
        session.add(comment)
        session.flush()

        record_channel_comment_learning_sample(session, comment)
        versions = list_learning_profile_versions(session, 1, 31, CHANNEL_COMMENT_SCENE)["items"]
        assert versions
        first_version_id = versions[-1]["id"]
        profile = session.scalar(select(TargetLearningProfile).where(TargetLearningProfile.target_id == 31))
        assert profile is not None
        profile.style_summary = "被手工改坏的画像"
        restore_learning_profile_version(session, 1, 31, first_version_id, actor="运营员", reason="恢复测试")
        session.commit()

        restored = session.scalar(select(TargetLearningProfile).where(TargetLearningProfile.target_id == 31))
        restored_versions = list_learning_profile_versions(session, 1, 31, CHANNEL_COMMENT_SCENE)["items"]

    assert restored is not None
    assert restored.style_summary != "被手工改坏的画像"
    assert restored_versions[0]["quality_snapshot"]["restored_from"] == versions[-1]["profile_version"]


def test_clear_learning_profile_records_version_snapshot():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="频道"))
        session.add(ChannelMessage(id=41, tenant_id=1, channel_target_id=31, message_id=9101, content_preview="频道消息"))
        comment = ChannelMessageComment(
            tenant_id=1,
            channel_target_id=31,
            channel_message_id=41,
            comment_message_id=8101,
            author_name="真人用户",
            content_preview="这个价格区间有人体验过吗？",
        )
        session.add(comment)
        session.flush()

        record_channel_comment_learning_sample(session, comment)
        clear_learning_profile(session, 1, 31, CHANNEL_COMMENT_SCENE, actor="运营员", reason="清空测试")
        session.commit()

        versions = list_learning_profile_versions(session, 1, 31, CHANNEL_COMMENT_SCENE)["items"]

    assert versions[0]["source_sample_count"] == 0
    assert versions[0]["quality_snapshot"] == {"cleared": True}


def test_manual_sample_status_update_rebuilds_profile():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="频道"))
        session.add(ChannelMessage(id=41, tenant_id=1, channel_target_id=31, message_id=9101, content_preview="频道消息"))
        comment = ChannelMessageComment(
            tenant_id=1,
            channel_target_id=31,
            channel_message_id=41,
            comment_message_id=8101,
            author_name="真人用户",
            content_preview="这个价格区间有人体验过吗？",
        )
        session.add(comment)
        session.flush()

        sample = record_channel_comment_learning_sample(session, comment)
        assert sample is not None
        update_learning_sample_status(session, 1, sample.id, "rejected", actor="运营员", reason="人工剔除机器人")
        session.commit()

        refreshed = session.get(TargetLearningSample, sample.id)
        profile = session.scalar(select(TargetLearningProfile).where(TargetLearningProfile.target_id == 31))

    assert refreshed is not None
    assert refreshed.learning_status == "rejected"
    assert refreshed.applied_profile_version is None
    assert profile is not None
    assert profile.source_sample_count == 0


def test_operation_target_detail_does_not_return_legacy_target_learning_preview():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="频道"))
        session.add(TargetLearningProfile(tenant_id=1, target_id=31, profile_scene=CHANNEL_COMMENT_SCENE, style_summary="真人画像摘要", source_sample_count=3, profile_version=2))
        session.commit()

        hidden = operation_target_detail(session, 1, 31)
        visible = operation_target_detail(session, 1, 31, include_learning_profile=True)

    assert hidden["learning_profile_preview"] == {}
    assert visible["learning_profile_preview"] == {}


def test_refresh_channel_learning_reports_sync_errors(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(
        "app.services.operations_center_learning.sync_operation_target_messages",
        lambda *_args, **_kwargs: {"detail": {"sync_error": "频道同步失败"}},
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=31, tenant_id=1, target_type="channel", tg_peer_id="-10031", title="频道"))
        session.commit()

        try:
            refresh_listener_learning(session, 1, "channel", 31, "pytest")
        except ValueError as exc:
            error = str(exc)
        else:
            error = ""

    assert "频道同步失败" in error


def test_ai_generation_quality_risks_are_frontend_renderable_list():
    risks = _quality_risks({"duplicate_risk": "语义重复", "hallucination_risk": ""})

    assert risks == ["语义重复"]
