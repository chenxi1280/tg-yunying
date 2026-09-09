"""Safety boundaries use synthetic advertisements, never production content."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.content_safety import BLOCKED_CONTENT_LABEL, CONTENT_BLOCK_REASON, content_screening_reason
from app.database import Base
from app.integrations.telegram import OutboundSegment, TelethonTelegramGateway
from app.integrations.telegram.telethon_clone_media import send_clone_media
from app.models import OperationTarget, Task, Tenant, TgGroup
from app.schemas.operations import ChannelMessageOut
from app.services.content_filters import rewrite_rejected_content
from app.services.content_screening_inventory import screening_inventory
from app.services.task_center.channel_source_policy import source_filter_reason
from app.services.tenant_learning_samples import _classify_sample


pytestmark = pytest.mark.no_postgres
ADVERTISEMENT = "性服务广告，预约：测试样本"


@pytest.mark.parametrize("value", [ADVERTISEMENT, "性 服 务 广 告，预 约：样本", "性\u200b服务，预约：样本",
                                  "ＥＳＣＯＲＴ ＳＥＲＶＩＣＥＳ book now", "楼凤资料，https://example.invalid"])
def test_advertisements_are_rejected_after_normalization(value):
    assert content_screening_reason(value) == CONTENT_BLOCK_REASON


@pytest.mark.parametrize("value", ["预约家政服务，价格200元", "明天去公园散步", "请警惕性交易广告，切勿转账",
                                  "性健康门诊预约咨询", "新政策出台，家政价格：200元", "网吧包夜预约：30元", "联系老师安排试听课", "楼凤广告是应当打击的违法内容"])
def test_normal_services_health_and_anti_scam_are_not_transaction_ads(value):
    assert not content_screening_reason(value)


def test_rejected_ad_is_not_laundered_by_keyword_rewriting():
    result = rewrite_rejected_content(None, tenant_id=1, group=None, content=ADVERTISEMENT)
    assert not result.ok and result.reason == CONTENT_BLOCK_REASON and not result.content


@pytest.mark.parametrize("kind", ["channel_view", "channel_like", "channel_comment"])
def test_every_channel_business_type_rejects_ad_even_without_metadata(kind):
    message = SimpleNamespace(content_preview=ADVERTISEMENT, source_metadata={})
    assert source_filter_reason(message, task_type=kind) == CONTENT_BLOCK_REASON


def test_final_gateway_methods_block_before_any_session_or_network_access(monkeypatch):
    gateway = TelethonTelegramGateway()
    def unexpected(*_args, **_kwargs):
        pytest.fail("rejected advertisement reached session/network access")
    monkeypatch.setattr("app.integrations.telegram.gateway.decrypt_session", unexpected)
    calls = [gateway._send_async(None, "test", ADVERTISEMENT, None, None),
             gateway._reply_channel_message_async(None, "test", message_id=1, content=ADVERTISEMENT, credentials=None),
             gateway._reply_channel_media_async(None, "test", message_id=1,
                 segment=OutboundSegment("image", caption=ADVERTISEMENT), credentials=None),
             gateway._send_raw_mtproto_message_async("test", ADVERTISEMENT, 1, None, None, None, None, None),
             send_clone_media(None, None, source_peer_id="source", target_peer_id="target",
                              items=[{"content": ADVERTISEMENT}], reply_to_message_id=None, target_top_message_id=None)]
    async def run():
        for call in calls:
            result = await call
            assert not result.ok and result.failure_type == CONTENT_BLOCK_REASON
            assert result.remote_mutation_started is False
    asyncio.run(run())


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="test"))
    session.flush()
    return session


def test_public_serialization_masks_ad_without_mutating_original_evidence():
    message = ChannelMessageOut(id=1, tenant_id=1, channel_target_id=1, message_id=1, message_url="",
                                content_preview=ADVERTISEMENT, comment_available=True,
                                published_at=None, created_at=datetime(2026, 9, 9))
    assert message.model_dump()["content_preview"] == BLOCKED_CONTENT_LABEL
    assert message.content_preview == ADVERTISEMENT


def test_learning_rejects_advertisement_even_when_it_is_media():
    with _session() as session:
        status, score, reason, _rule = _classify_sample(session, 1, text=ADVERTISEMENT,
            sender_username="", sender_name="reader", is_bot=False, is_media=True)
        assert (status, score, reason) == ("rejected", 0, CONTENT_BLOCK_REASON)


def test_incoming_ad_cannot_trigger_group_bot_control_or_learning(monkeypatch):
    from app.services import group_listener_context_writer as writer
    monkeypatch.setattr(writer, "_lock_group_speaker_state", lambda *_: None)
    monkeypatch.setattr("app.services.group_listener_ai_context.ai_context_tracking_enabled", lambda *_: False)
    def unexpected(*_args, **_kwargs):
        pytest.fail("advertisement reached bot control or learning")
    monkeypatch.setattr(writer, "_process_group_bot_control_event", unexpected)
    with _session() as session:
        group = TgGroup(id=1, tenant_id=1, title="test", tg_peer_id="-1001")
        snapshot = SimpleNamespace(content=ADVERTISEMENT, remote_message_id="123")
        assert writer.insert_context_snapshots(session, group, None, [snapshot], ignored_sender=unexpected,
                                               create_source_media=True, learning_scene="group_chat") == 0


def test_inventory_uses_exact_peer_relationship_and_hash_only_evidence():
    from app.models import ChannelMessage
    with _session() as session:
        target = OperationTarget(id=1, tenant_id=1, tg_peer_id="-1001", title="test", username="testtarget")
        task = Task(id="dependent", tenant_id=1, name="test", type="channel_view", status="running",
                    type_config={"target_channel_id": 1})
        session.add_all([target, task, ChannelMessage(id=1, tenant_id=1, channel_target_id=1,
                         message_id=1, content_preview=ADVERTISEMENT)])
        session.flush()
        result = screening_inventory(session, 1)
        assert [row["id"] for row in result["targets"]] == [1]
        assert [row["id"] for row in result["tasks"]] == ["dependent"]
        assert "性服务" not in str(result)
        assert result == screening_inventory(session, 1)
