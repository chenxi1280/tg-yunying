from __future__ import annotations

import asyncio
from datetime import timedelta
from io import BytesIO
from os import utime
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.config import get_settings
from app.integrations.telegram import DeveloperAppCredentials, GroupMessageSnapshot, SendResult, TelethonTelegramGateway
from app.models import AccountStatus, Action, AuditLog, ChannelMessage, ContentKeywordRule, FailureType, GroupContextMessage, Material, MaterialAssetVersion, MaterialTgRefVersion, MessageTask, OperationPlanTemplate, OperationTarget, RuleSetVersion, SourceMediaAsset, Task, Tenant, TgAccount, TgAccountSecurityBatchItem, TgGroup, TgGroupAccount
from app.schemas.operations_center import RuleSetCreate, RuleSetVersionCreate
from app.schemas.ai_config import MaterialCreate, MaterialUpdate
from app.services import ai_config as ai_config_service
from app.services.ai_config import create_material, create_uploaded_material, create_uploaded_materials, disable_material, list_materials, material_cache_health, restore_material, update_material
from app.services.material_ingestion import deep_probe_material_url
from app.services.messages import build_outbound_segments
from app.services.operations_center import (
    copy_rule_set_version,
    create_rule_set,
    create_rule_set_version,
    list_rule_set_bound_tasks,
    publish_rule_set_version,
    rollback_rule_set_version,
    rule_center_summary,
    test_rules as preview_rules,
    update_rule_set_config,
)
from app.services.rule_engine import apply_output_policy, bound_rule_version, transform_content
from app.services.task_center.executors.group_relay import build_plan as build_relay_plan, effective_relay_config
from app.services.task_center.executors.group_ai_chat import build_plan as build_ai_chat_plan
from app.services.task_center.executors.common import channel_scope
from app.services._common import _now
from app.services.group_listeners import is_listener_ignored_sender
from app.services.source_media import (
    WAITING_MATERIAL_CACHE,
    drain_source_media_cache,
    register_action_waiting_for_source_media,
    source_media_cached_event,
)
from app.services.material_cache import drain_material_cache
from app.services.temp_files import TEMP_FILE_TTL_SECONDS, cleanup_temp_files, temp_dir


def test_rule_set_create_persists_task_scope_and_output_checks():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        created = create_rule_set(
            session,
            1,
            RuleSetCreate(
                name="AI 回复安全规则",
                task_types=["group_ai_chat"],
                default_policy={"output_failure": "transform_once_drop"},
                output_checks={"forbidden_keywords": ["引流"], "failure_strategy": "transform_once_drop"},
                transforms={"keyword_replacements": {"引流": "活动"}},
            ),
            "tester",
        )

    assert created.task_types == ["group_ai_chat"]
    assert created.default_policy["output_failure"] == "transform_once_drop"
    assert created.versions[0].output_checks["forbidden_keywords"] == ["引流"]


def test_rule_tester_validates_ai_candidates_one_by_one():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        rule_set = create_rule_set(
            session,
            1,
            RuleSetCreate(
                name="AI 候选校验",
                task_types=["group_ai_chat"],
                output_checks={"forbidden_keywords": ["风险"], "failure_strategy": "transform_once_drop"},
                transforms={"keyword_replacements": {"风险": "正常"}},
            ),
            "tester",
        )
        result = preview_rules(
            session,
            1,
            "用户消息",
            test_type="group_ai_chat",
            candidates=["第一条正常", "第二条风险内容"],
            rule_set_version_id=rule_set.active_version_id,
        )

    assert [item.passed for item in result.output_candidates] == [True, True]
    assert result.output_candidates[1].action == "transform"
    assert result.output_candidates[1].transformed_text == "第二条正常内容"


def test_rule_tester_simulates_material_cache_edges():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        album_result = preview_rules(session, 1, "相册消息", simulation_scenario="album_one_failed")
        queue_result = preview_rules(session, 1, "队列保护", simulation_scenario="queue_overflow")
        late_result = preview_rules(session, 1, "迟到事件", simulation_scenario="timeout_then_cached")
        pending_result = preview_rules(session, 1, "待缓存", simulation_scenario="pending_cache")
        old_event_result = preview_rules(session, 1, "旧事件", simulation_scenario="late_cache_event")

    assert [step.status for step in album_result.simulation_steps] == ["ready", "album_segment_failed", "ready"]
    assert album_result.simulation_steps[1].action == "剔除失败图"
    assert queue_result.simulation_steps[0].status == "material_cache_wait_queue_full"
    assert "不创建人工缓存动作" in queue_result.simulation_steps[0].reason
    assert late_result.simulation_steps[1].status == "late_event"
    assert late_result.simulation_steps[1].action == "拒绝补发"
    assert pending_result.simulation_steps[0].status == "pending_cache"
    assert pending_result.simulation_steps[0].action == "等待本轮超时或按规则降级"
    assert old_event_result.simulation_steps[0].status == "stale_event"
    assert old_event_result.simulation_steps[0].action == "拒绝唤醒"


def test_rule_material_policy_selects_ready_material_for_preview_and_ai_action(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat.generate_group_messages",
        lambda *_args, **_kwargs: (["素材规则触发"], 0),
    )
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(id=101, tenant_id=1, display_name="AI号", phone_masked="101", status=AccountStatus.ACTIVE.value, session_ciphertext="session")
        group = TgGroup(id=201, tenant_id=1, tg_peer_id="-100201", title="素材群", auth_status="已授权运营", can_send=True, listener_interval_seconds=0)
        session.add_all([account, group, TgGroupAccount(tenant_id=1, group_id=201, account_id=101, can_send=True)])
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
            )
        )
        rule_set = create_rule_set(
            session,
            1,
            RuleSetCreate(
                name="素材规则",
                task_types=["group_ai_chat"],
                routing={
                    "material_policy": {
                        "enabled": True,
                        "material_type": "表情包",
                        "required_tags": ["围观"],
                        "action": "append_media",
                        "fallback": "text_only",
                    }
                },
            ),
            "tester",
        )
        preview = preview_rules(session, 1, "素材规则触发", test_type="group_ai_chat", rule_set_version_id=rule_set.active_version_id)
        task = Task(
            id="ai-material-rule",
            tenant_id=1,
            name="AI素材规则",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [101], "cooldown_per_account_minutes": 0},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0},
            type_config={
                "target_group_id": 201,
                "messages_per_round": 1,
                "participation_rate": 1,
                "rule_set_version_id": rule_set.active_version_id,
            },
            stats={"force_bootstrap_once": True},
        )
        session.add(task)
        session.commit()

        assert build_ai_chat_plan(session, task) == 1
        action = session.scalar(select(Action).where(Action.task_id == task.id))
        action_payload = action.payload

    assert preview.material_candidate_count == 1
    assert preview.material_selected_id == 9301
    assert action_payload["media_segments"][0]["material_id"] == 9301
    assert action_payload["media_segments"][0]["source"] == "tg-cache://cache-peer/9301"
    assert action_payload["rule_trace"]["material_id"] == 9301


def test_source_media_waiting_rejects_stale_event_and_queue_overflow():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        task = Task(id="relay-media", tenant_id=1, name="媒体转发", type="group_relay", status="running")
        action = Action(id="action-wait", tenant_id=1, task_id=task.id, task_type=task.type, action_type="send_message", status="pending", payload={"message_text": "带图转发"})
        asset = SourceMediaAsset(id="asset-1", tenant_id=1, source_message_id="src-1", media_group_index=1, cache_status="pending_cache", cache_version=2)
        session.add_all([task, action, asset])
        session.flush()

        assert register_action_waiting_for_source_media(session, action, [asset.id], queue_limit=10)
        assert action.status == WAITING_MATERIAL_CACHE
        assert source_media_cached_event(session, source_media_asset_id=asset.id, cache_peer_id="cache", cache_message_id="old", cache_version=1) == 0
        assert action.status == WAITING_MATERIAL_CACHE
        assert asset.cache_status == "pending_cache"

        assert source_media_cached_event(session, source_media_asset_id=asset.id, cache_peer_id="cache", cache_message_id="new", cache_version=2) == 1
        assert action.status == "pending"
        assert action.payload["media_segments"][0]["source"] == "tg-cache://cache/new"

        overflow_action = Action(id="action-overflow", tenant_id=1, task_id=task.id, task_type=task.type, action_type="send_message", status="pending", payload={"message_text": "队列满"})
        waiting_filler = Action(id="action-existing-wait", tenant_id=1, task_id=task.id, task_type=task.type, action_type="send_message", status=WAITING_MATERIAL_CACHE, payload={"message_text": "已有等待"})
        overflow_asset = SourceMediaAsset(id="asset-overflow", tenant_id=1, source_message_id="src-2", cache_status="pending_cache")
        session.add_all([overflow_action, waiting_filler, overflow_asset])
        session.flush()

        assert not register_action_waiting_for_source_media(session, overflow_action, [overflow_asset.id], queue_limit=1)
        assert overflow_action.status == "skipped"
        assert overflow_action.result["error_code"] == "material_cache_wait_queue_full"
        assert overflow_asset.cache_status == "cache_failed"


def test_group_relay_skips_existing_source_channel_self_messages(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.services.task_center.executors.group_relay.should_collect_listener", lambda *args, **kwargs: False)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="发送号", phone_masked="101", status=AccountStatus.ACTIVE.value))
        source = TgGroup(id=201, tenant_id=1, tg_peer_id="-100201", title="来源频道", auth_status="已授权运营")
        target = TgGroup(id=202, tenant_id=1, tg_peer_id="-100202", title="目标群", auth_status="已授权运营")
        session.add_all([source, target])
        session.add_all(
            [
                TgGroupAccount(tenant_id=1, group_id=source.id, account_id=101, can_send=True, is_listener=True),
                TgGroupAccount(tenant_id=1, group_id=target.id, account_id=101, can_send=True),
                GroupContextMessage(
                    tenant_id=1,
                    group_id=source.id,
                    listener_account_id=101,
                    sender_peer_id="201",
                    sender_name=source.title,
                    content="频道机器发送的内容不应进入转发计划",
                    remote_message_id="source-channel-self-1",
                ),
            ]
        )
        task = Task(
            id="relay-source-self",
            tenant_id=1,
            name="屏蔽来源频道自身消息",
            type="group_relay",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0},
            type_config={
                "source_groups": [{"group_id": source.id, "is_active": True}],
                "target_group_id": target.id,
                "content_mode": "raw",
            },
        )
        session.add(task)
        session.commit()

        assert build_relay_plan(session, task) == 0
        assert session.query(Action).filter_by(task_id=task.id, action_type="send_message").count() == 0


def test_group_relay_keeps_real_users_when_id_or_name_matches_source_channel(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.services.task_center.executors.group_relay.should_collect_listener", lambda *args, **kwargs: False)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="发送号", phone_masked="101", status=AccountStatus.ACTIVE.value))
        source = TgGroup(id=201, tenant_id=1, tg_peer_id="-100201", title="来源频道", auth_status="已授权运营")
        target = TgGroup(id=202, tenant_id=1, tg_peer_id="-100202", title="目标群", auth_status="已授权运营")
        session.add_all([source, target])
        session.add_all(
            [
                TgGroupAccount(tenant_id=1, group_id=source.id, account_id=101, can_send=True, is_listener=True),
                TgGroupAccount(tenant_id=1, group_id=target.id, account_id=101, can_send=True),
                GroupContextMessage(
                    tenant_id=1,
                    group_id=source.id,
                    listener_account_id=101,
                    sender_peer_id="201",
                    sender_name="真实用户",
                    content="真实用户 id 和频道 suffix 碰撞也要转发",
                    remote_message_id="real-user-id-collision",
                ),
                GroupContextMessage(
                    tenant_id=1,
                    group_id=source.id,
                    listener_account_id=101,
                    sender_peer_id="real-user-peer",
                    sender_name=source.title,
                    content="真实用户昵称等于来源频道标题也要转发",
                    remote_message_id="real-user-name-collision",
                ),
            ]
        )
        task = Task(
            id="relay-real-collisions",
            tenant_id=1,
            name="保留真实来源用户",
            type="group_relay",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0},
            type_config={
                "source_groups": [{"group_id": source.id, "is_active": True}],
                "target_group_id": target.id,
                "content_mode": "raw",
            },
        )
        session.add(task)
        session.commit()

        assert build_relay_plan(session, task) == 2
        payloads = [item.payload for item in session.query(Action).filter_by(task_id=task.id, action_type="send_message").all()]
        assert {payload["original_text"] for payload in payloads} == {
            "真实用户 id 和频道 suffix 碰撞也要转发",
            "真实用户昵称等于来源频道标题也要转发",
        }


def test_listener_does_not_ignore_distinct_channel_with_same_title():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        source = TgGroup(id=201, tenant_id=1, tg_peer_id="-100201", title="来源频道", auth_status="已授权运营")
        session.add(source)
        session.commit()

        snapshot = GroupMessageSnapshot(
            remote_message_id="same-title-other-channel",
            sender_peer_id="999",
            sender_peer_type="channel",
            sender_name=source.title,
            content="同名其它频道的内容不能被来源自身过滤误伤",
        )

        assert is_listener_ignored_sender(session, source, snapshot) is False


def test_source_media_cache_worker_uploads_and_wakes_waiting_action(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("SOURCE_MEDIA_CACHE_PEER_ID", "cache-peer")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.services.developer_apps.credentials_for_account",
        lambda *_args, **_kwargs: DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1, app_name="pytest"),
    )
    monkeypatch.setattr(
        "app.services.source_media.gateway.cache_source_media",
        lambda *args, **kwargs: SendResult(True, remote_message_id="301"),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="监听号", phone_masked="101", status=AccountStatus.ACTIVE.value, session_ciphertext="session"))
        task = Task(id="relay-worker", tenant_id=1, name="媒体缓存 worker", type="group_relay", status="running")
        action = Action(id="action-worker", tenant_id=1, task_id=task.id, task_type=task.type, action_type="send_message", status="pending", payload={"message_text": "worker"})
        asset = SourceMediaAsset(id="asset-worker", tenant_id=1, listener_account_id=101, source_peer_id="-1001", source_message_id="55", cache_status="pending_cache")
        session.add_all([task, action, asset])
        session.flush()
        register_action_waiting_for_source_media(session, action, [asset.id])
        session.commit()

    def factory():
        return Session(engine)

    assert drain_source_media_cache(factory, limit=10) == 1

    with Session(engine) as session:
        action = session.get(Action, "action-worker")
        asset = session.get(SourceMediaAsset, "asset-worker")
        assert asset.cache_status == "ready"
        assert asset.cache_peer_id == "cache-peer"
        assert asset.cache_message_id == "301"
        assert action.status == "pending"
        assert action.payload["media_segments"][0]["source"] == "tg-cache://cache-peer/301"

    get_settings.cache_clear()


def test_source_media_cache_worker_marks_missing_cache_peer_observable(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.delenv("SOURCE_MEDIA_CACHE_PEER_ID", raising=False)
    get_settings.cache_clear()
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SourceMediaAsset(id="asset-no-peer", tenant_id=1, source_message_id="src-no-peer", cache_status="pending_cache"))
        session.commit()

    def factory():
        return Session(engine)

    assert drain_source_media_cache(factory, limit=10) == 0
    with Session(engine) as session:
        asset = session.get(SourceMediaAsset, "asset-no-peer")
        assert asset.cache_status == "pending_cache"
        assert asset.failure_reason == "cache_peer_unavailable"

    get_settings.cache_clear()


def test_material_cache_worker_marks_media_material_ready(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("MATERIAL_CACHE_PEER_ID", "material-cache-peer")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.services.developer_apps.credentials_for_account",
        lambda *_args, **_kwargs: DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1, app_name="pytest"),
    )
    monkeypatch.setattr(
        "app.services.material_cache.gateway.cache_material_source",
        lambda *args, **kwargs: SendResult(True, remote_message_id="material-501"),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="缓存号", phone_masked="101", status=AccountStatus.ACTIVE.value, session_ciphertext="session"))
        material = Material(
            id=9001,
            tenant_id=1,
            title="待缓存图片",
            material_type="图片",
            content="https://trusted.example.com/material.png",
            cache_ready_status="not_cached",
        )
        session.add(material)
        session.commit()

    def factory():
        return Session(engine)

    assert drain_material_cache(factory, limit=10) == 1

    with Session(engine) as session:
        material = session.get(Material, 9001)
        assert material.cache_ready_status == "ready"
        assert material.tg_cache_account_id == 101
        assert material.tg_cache_peer_id == "material-cache-peer"
        assert material.tg_cache_message_id == "material-501"
        assert material.last_cache_error == ""

    get_settings.cache_clear()


def test_material_cache_worker_tries_next_active_account_when_first_cannot_cache(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("MATERIAL_CACHE_PEER_ID", "material-cache-peer")
    get_settings.cache_clear()
    attempted_accounts: list[int] = []
    monkeypatch.setattr(
        "app.services.developer_apps.credentials_for_account",
        lambda *_args, **_kwargs: DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1, app_name="pytest"),
    )

    def fake_cache_material(account_id, *args, **kwargs):
        attempted_accounts.append(account_id)
        if account_id == 102:
            return SendResult(True, remote_message_id="material-502")
        return SendResult(False, failure_type="GROUP_PERMISSION_DENIED", detail="no cache permission")

    monkeypatch.setattr("app.services.material_cache.gateway.cache_material_source", fake_cache_material)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="无权限缓存号", phone_masked="101", status=AccountStatus.ACTIVE.value, health_score=95, session_ciphertext="session-101"))
        session.add(TgAccount(id=102, tenant_id=1, display_name="有权限缓存号", phone_masked="102", status=AccountStatus.ACTIVE.value, health_score=95, session_ciphertext="session-102"))
        session.add(Material(id=9002, tenant_id=1, title="待缓存图片", material_type="图片", content="https://trusted.example.com/material.png", cache_ready_status="not_cached"))
        session.commit()

    def factory():
        return Session(engine)

    assert drain_material_cache(factory, limit=10) == 1

    with Session(engine) as session:
        material = session.get(Material, 9002)

    assert attempted_accounts == [101, 102]
    assert material.cache_ready_status == "ready"
    assert material.tg_cache_account_id == 102
    assert material.tg_cache_message_id == "material-502"

    get_settings.cache_clear()


def test_material_cache_worker_prefers_configured_cache_account(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    attempted_accounts: list[int] = []
    monkeypatch.setattr(
        "app.services.developer_apps.credentials_for_account",
        lambda *_args, **_kwargs: DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1, app_name="pytest"),
    )
    monkeypatch.setattr(
        "app.services.material_cache.gateway.cache_material_source",
        lambda account_id, *args, **kwargs: attempted_accounts.append(account_id) or SendResult(True, remote_message_id="material-503"),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="自动优先号", phone_masked="101", status=AccountStatus.ACTIVE.value, health_score=99, session_ciphertext="session-101"))
        session.add(TgAccount(id=102, tenant_id=1, display_name="指定缓存号", phone_masked="102", status=AccountStatus.ACTIVE.value, health_score=80, session_ciphertext="session-102"))
        ai_config_service.update_material_cache_config(
            session,
            tenant_id=1,
            material_cache_input="https://t.me/cache_target",
            source_media_cache_input=None,
            material_cache_account_id=102,
            actor="pytest",
        )
        session.add(Material(id=9003, tenant_id=1, title="待缓存图片", material_type="图片", content="https://trusted.example.com/material.png", cache_ready_status="not_cached"))
        session.commit()

    def factory():
        return Session(engine)

    assert drain_material_cache(factory, limit=10) == 1

    with Session(engine) as session:
        material = session.get(Material, 9003)

    assert attempted_accounts == [102]
    assert material.cache_ready_status == "ready"
    assert material.tg_cache_account_id == 102


def test_batch_uploaded_materials_create_one_row_per_file():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        materials = create_uploaded_materials(
            session,
            tenant_id=1,
            title="活群图",
            material_type="图片",
            tags="活群,表情",
            caption="",
            files=[
                ("one.png", "image/png", b"png-one"),
                ("two.png", "image/png", b"png-two"),
            ],
            actor="tester",
        )

    assert [item.title for item in materials] == ["活群图-one", "活群图-two"]
    assert [item.cache_ready_status for item in materials] == ["not_cached", "not_cached"]
    assert all(item.source_kind == "upload" for item in materials)
    assert all(item.file_size > 0 for item in materials)


def test_material_cache_config_normalizes_admin_links_and_overrides_env(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("MATERIAL_CACHE_PEER_ID", "env-material-cache")
    monkeypatch.setenv("SOURCE_MEDIA_CACHE_PEER_ID", "env-source-cache")
    get_settings.cache_clear()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

        saved = ai_config_service.update_material_cache_config(
            session,
            tenant_id=1,
            material_cache_input="https://t.me/c/1234567890/55397",
            source_media_cache_input="@example_cache",
            actor="pytest",
        )
        health = material_cache_health(session, 1)
        material_peer = ai_config_service.resolve_material_cache_peer_id(session, 1)
        source_peer = ai_config_service.resolve_source_media_cache_peer_id(session, 1)
        audit_actions = [row.action for row in session.scalars(select(AuditLog)).all()]

    assert saved.material_cache.raw_input == "https://t.me/c/1234567890/55397"
    assert saved.material_cache.normalized_peer == "-1001234567890"
    assert saved.material_cache.source == "saved"
    assert "已保存为 -1001234567890" in saved.material_cache.last_error
    assert "没有可用缓存账号" in saved.material_cache.last_error
    assert saved.source_media_cache.raw_input == "@example_cache"
    assert saved.source_media_cache.normalized_peer == "@example_cache"
    assert "已保存为 @example_cache" in saved.source_media_cache.last_error
    assert "没有可用缓存账号" in saved.source_media_cache.last_error
    assert material_peer == "-1001234567890"
    assert source_peer == "@example_cache"
    assert health.material_cache_peer_configured is True
    assert health.source_media_cache_peer_configured is True
    assert "更新素材缓存频道配置" in audit_actions

    get_settings.cache_clear()


def test_cache_config_normalizes_public_link_and_explains_probe_failure(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        "app.services.developer_apps.credentials_for_account",
        lambda *_args, **_kwargs: DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1, app_name="pytest"),
    )
    monkeypatch.setattr(ai_config_service.gateway, "probe_target_capabilities", lambda *args, **kwargs: type("_Probe", (), {"ok": False})())

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="缓存号", phone_masked="101", status=AccountStatus.ACTIVE.value, session_ciphertext="session"))
        session.commit()
        result = ai_config_service.update_material_cache_config(
            session,
            tenant_id=1,
            material_cache_input="https://t.me/yangyunipingdap",
            source_media_cache_input=None,
            actor="pytest",
        )

    assert result.material_cache.raw_input == "https://t.me/yangyunipingdap"
    assert result.material_cache.normalized_peer == "@yangyunipingdap"
    assert "已保存为 @yangyunipingdap" in result.material_cache.last_error
    assert "加入该频道" in result.material_cache.last_error
    assert "发消息/发帖权限" in result.material_cache.last_error


def test_cache_config_uses_later_active_account_when_first_probe_fails(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    probed_accounts: list[int] = []
    monkeypatch.setattr(
        "app.services.developer_apps.credentials_for_account",
        lambda *_args, **_kwargs: DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1, app_name="pytest"),
    )

    def fake_probe(account_id, *args, **kwargs):
        probed_accounts.append(account_id)
        return type("_Probe", (), {"ok": account_id == 102})()

    monkeypatch.setattr(ai_config_service.gateway, "probe_target_capabilities", fake_probe)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="无权限缓存号", phone_masked="101", status=AccountStatus.ACTIVE.value, health_score=95, session_ciphertext="session-101"))
        session.add(TgAccount(id=102, tenant_id=1, display_name="有权限缓存号", phone_masked="102", status=AccountStatus.ACTIVE.value, health_score=95, session_ciphertext="session-102"))
        session.commit()
        result = ai_config_service.update_material_cache_config(
            session,
            tenant_id=1,
            material_cache_input="https://t.me/yangyunipingdap",
            source_media_cache_input=None,
            actor="pytest",
        )

    assert probed_accounts == [101, 102]
    assert result.material_cache.normalized_peer == "@yangyunipingdap"
    assert result.material_cache.last_error == ""


def test_cache_config_prefers_selected_cache_account_for_probe(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    probed_accounts: list[int] = []
    monkeypatch.setattr(
        "app.services.developer_apps.credentials_for_account",
        lambda *_args, **_kwargs: DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1, app_name="pytest"),
    )

    def fake_probe(account_id, *args, **kwargs):
        probed_accounts.append(account_id)
        return type("_Probe", (), {"ok": True})()

    monkeypatch.setattr(ai_config_service.gateway, "probe_target_capabilities", fake_probe)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="自动优先号", phone_masked="101", status=AccountStatus.ACTIVE.value, health_score=99, session_ciphertext="session-101"))
        session.add(TgAccount(id=102, tenant_id=1, display_name="指定缓存号", phone_masked="102", status=AccountStatus.ACTIVE.value, health_score=80, session_ciphertext="session-102"))
        session.commit()
        result = ai_config_service.update_material_cache_config(
            session,
            tenant_id=1,
            material_cache_input="https://t.me/yangyunipingdap",
            source_media_cache_input=None,
            material_cache_account_id=102,
            actor="pytest",
        )

    assert probed_accounts == [102]
    assert result.cache_account is not None
    assert result.cache_account.id == 102
    assert result.cache_account.display_name == "指定缓存号"
    assert result.material_cache.last_error == ""


def test_cache_config_rejects_cache_account_from_another_tenant():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Tenant(id=2, name="其它空间"))
        session.add(TgAccount(id=202, tenant_id=2, display_name="其它租户账号", phone_masked="202", status=AccountStatus.ACTIVE.value, session_ciphertext="session-202"))
        session.commit()
        try:
            ai_config_service.update_material_cache_config(
                session,
                tenant_id=1,
                material_cache_input="https://t.me/yangyunipingdap",
                source_media_cache_input=None,
                material_cache_account_id=202,
                actor="pytest",
            )
        except ValueError as exc:
            error = str(exc)
        else:
            error = ""

    assert error == "缓存执行账号不存在或不属于当前租户"


def test_zip_material_import_persists_result_and_skips_invalid_entries():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    archive = BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("valid/one.png", b"\x89PNG\r\n\x1a\nimage-one")
        zip_file.writestr("__MACOSX/._one.png", b"ignored")
        zip_file.writestr("notes/readme.txt", b"not image")
        zip_file.writestr("fake/not-real.png", b"not actually a png")
        zip_file.writestr("duplicates/one-copy.png", b"\x89PNG\r\n\x1a\nimage-one")
        zip_file.writestr("oversize/big.jpg", b"\xff\xd8" + b"x" * (500 * 1024))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

        result = ai_config_service.create_material_zip_import(
            session,
            tenant_id=1,
            title="活动图包",
            material_type="图片",
            tags="活动,图包",
            caption="",
            filename="materials.zip",
            data=archive.getvalue(),
            actor="pytest",
        )
        persisted = ai_config_service.get_material_import_result(session, tenant_id=1, import_id=result.import_id)
        materials = list_materials(session, 1)

    assert result.import_id == persisted.import_id
    assert result.status == "completed"
    assert result.success_count == 1
    assert result.skipped_count == 5
    assert result.oversize_count == 1
    assert [item.file_name for item in result.items if item.status == "created"] == ["valid/one.png"]
    skipped_reasons = {item.reason for item in result.items if item.status == "skipped"}
    assert skipped_reasons >= {"系统目录已跳过", "素材文件类型不支持", "重复文件已跳过"}
    assert any("素材文件过大" in reason for reason in skipped_reasons)
    assert [material.title for material in materials] == ["活动图包-one"]
    assert materials[0].tags == "活动,图包"
    assert materials[0].source_kind == "upload"


def test_zip_material_import_rejects_oversize_entry_before_reading(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    archive = BytesIO()
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zip_file:
        zip_file.writestr("oversize/big.jpg", b"\xff\xd8" + b"x" * (600 * 1024))

    def fail_if_reading_oversize_entry(self, name, *args, **kwargs):
        raise AssertionError("oversize zip entry should be rejected before read")

    monkeypatch.setattr(ZipFile, "read", fail_if_reading_oversize_entry)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

        result = ai_config_service.create_material_zip_import(
            session,
            tenant_id=1,
            title="超大图包",
            material_type="图片",
            tags="",
            caption="",
            filename="oversize.zip",
            data=archive.getvalue(),
            actor="pytest",
        )

    assert result.success_count == 0
    assert result.skipped_count == 1
    assert result.oversize_count == 1
    assert result.items[0].file_size == 614402
    assert "素材文件过大" in result.items[0].reason


def test_channel_view_scope_does_not_fetch_new_messages_when_listener_disabled(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    def fail_collect(*args, **kwargs):
        raise AssertionError("disabled channel listener should not collect new messages")

    monkeypatch.setattr("app.services.task_center.executors.common.collect_channel_messages", fail_collect)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=10, tenant_id=1, display_name="采集账号", phone_masked="+861***0010", status=AccountStatus.ACTIVE.value, session_ciphertext="session"))
        session.add(OperationTarget(id=100, tenant_id=1, target_type="channel", tg_peer_id="pytest_channel", title="pytest频道"))
        task = Task(
            id="task-no-new-listener",
            tenant_id=1,
            name="关闭新帖监听浏览",
            type="channel_view",
            status="running",
            type_config={"target_channel_id": 100, "message_scope": "dynamic_new", "message_count": 1, "listen_new_messages": False},
        )
        session.add(task)
        session.add(ChannelMessage(id=200, tenant_id=1, channel_target_id=100, message_id=9001, content_preview="已有消息", published_at=_now()))
        session.commit()

        channel, messages = channel_scope(session, task, task.type_config)

    assert channel is not None
    assert [message.message_id for message in messages] == [9001]


def test_cache_workers_prefer_saved_cache_config_over_env(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("MATERIAL_CACHE_PEER_ID", "env-material-cache")
    monkeypatch.setenv("SOURCE_MEDIA_CACHE_PEER_ID", "env-source-cache")
    get_settings.cache_clear()
    material_cache_peers: list[str] = []
    source_cache_peers: list[str] = []
    monkeypatch.setattr(
        "app.services.developer_apps.credentials_for_account",
        lambda *_args, **_kwargs: DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1, app_name="pytest"),
    )
    monkeypatch.setattr(
        "app.services.material_cache.gateway.cache_material_source",
        lambda *args, **kwargs: material_cache_peers.append(args[2]) or SendResult(True, remote_message_id="material-701"),
    )
    monkeypatch.setattr(
        "app.services.source_media.gateway.cache_source_media",
        lambda *args, **kwargs: source_cache_peers.append(args[3]) or SendResult(True, remote_message_id="source-701"),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="缓存号", phone_masked="101", status=AccountStatus.ACTIVE.value, session_ciphertext="session"))
        session.add(Material(id=9701, tenant_id=1, title="待缓存", material_type="图片", content="https://trusted.example.com/a.png", cache_ready_status="not_cached"))
        session.add(SourceMediaAsset(id="saved-config-source", tenant_id=1, listener_account_id=101, source_peer_id="-1001", source_message_id="55", cache_status="pending_cache"))
        session.commit()
        ai_config_service.update_material_cache_config(
            session,
            tenant_id=1,
            material_cache_input="https://t.me/c/222333444/1",
            source_media_cache_input="https://t.me/source_cache",
            actor="pytest",
        )

    def factory():
        return Session(engine)

    assert drain_material_cache(factory, limit=10) == 1
    assert drain_source_media_cache(factory, limit=10) == 1
    assert material_cache_peers == ["-100222333444"]
    assert source_cache_peers == ["@source_cache"]

    get_settings.cache_clear()


def test_cache_config_records_friendly_error_when_probe_fails(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        "app.services.developer_apps.credentials_for_account",
        lambda *_args, **_kwargs: DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1, app_name="pytest"),
    )
    monkeypatch.setattr(ai_config_service.gateway, "probe_target_capabilities", lambda *args, **kwargs: type("_Probe", (), {"ok": False})())

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="缓存号", phone_masked="101", status=AccountStatus.ACTIVE.value, session_ciphertext="session"))
        session.commit()
        result = ai_config_service.update_material_cache_config(
            session,
            tenant_id=1,
            material_cache_input="@broken_cache",
            source_media_cache_input="@source_cache",
            actor="pytest",
        )

    assert "已保存为 @broken_cache" in result.material_cache.last_error
    assert "发消息/发帖权限" in result.material_cache.last_error
    assert "已保存为 @source_cache" in result.source_media_cache.last_error
    assert "发消息/发帖权限" in result.source_media_cache.last_error


def test_telethon_cache_probe_fails_when_permissions_cannot_be_confirmed(monkeypatch):
    gateway = TelethonTelegramGateway()

    class FakeClient:
        async def is_user_authorized(self) -> bool:
            return True

        async def get_permissions(self, target, user):  # noqa: ANN001 - mirrors Telethon shape.
            raise RuntimeError("participant permissions unavailable")

    async def fake_get_or_create_client(credentials, raw_session):  # noqa: ANN001 - mirrors gateway hook.
        return FakeClient()

    async def fake_resolve_target(client, peer_id, *, group_id=0):  # noqa: ANN001 - mirrors Telethon helper.
        return type("_Target", (), {"default_banned_rights": None})()

    monkeypatch.setattr(gateway, "_get_or_create_client", fake_get_or_create_client)
    monkeypatch.setattr("app.integrations.telegram.gateway.resolve_telethon_target", fake_resolve_target)

    result = asyncio.run(
        gateway._probe_target_capabilities_async(
            "raw-session",
            "@cache",
            "channel",
            DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1, app_name="pytest"),
        )
    )

    assert result.ok is False
    assert result.detail == "缓存频道不可访问 / 账号无权限"


def test_telethon_probe_preserves_required_channel_reference(monkeypatch):
    gateway = TelethonTelegramGateway()

    class FakeClient:
        async def is_user_authorized(self) -> bool:
            return True

        async def get_permissions(self, target, user):  # noqa: ANN001 - mirrors Telethon shape.
            raise RuntimeError(
                "Must join @RequiredChannel before sending. "
                "The channel specified is private and you lack permission to access it "
                "(caused by SendMessageRequest)"
            )

    async def fake_get_or_create_client(credentials, raw_session):  # noqa: ANN001 - mirrors gateway hook.
        return FakeClient()

    async def fake_resolve_target(client, peer_id, *, group_id=0):  # noqa: ANN001 - mirrors Telethon helper.
        return type("_Target", (), {"default_banned_rights": None})()

    monkeypatch.setattr(gateway, "_get_or_create_client", fake_get_or_create_client)
    monkeypatch.setattr("app.integrations.telegram.gateway.resolve_telethon_target", fake_resolve_target)

    result = asyncio.run(
        gateway._probe_target_capabilities_async(
            "raw-session",
            "@cache",
            "channel",
            DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1, app_name="pytest"),
        )
    )

    assert result.ok is False
    assert "@RequiredChannel" in result.detail


def test_zip_avatar_pack_import_maps_to_image_materials():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    archive = BytesIO()
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr("avatar.jpg", b"\xff\xd8avatar-image")

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        result = ai_config_service.create_material_zip_import(
            session,
            tenant_id=1,
            title="头像包A",
            material_type="头像包",
            tags="头像",
            caption="",
            filename="avatars.zip",
            data=archive.getvalue(),
            actor="pytest",
        )
        materials = list_materials(session, 1)

    assert result.import_type == "avatar_pack"
    assert result.target_group_name == "头像包A"
    assert result.success_count == 1
    assert materials[0].material_type == "图片"
    assert materials[0].title == "头像包A-avatar"


def test_material_cache_worker_respects_flood_wait_retry_time(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("MATERIAL_CACHE_PEER_ID", "material-cache-peer")
    get_settings.cache_clear()
    calls: list[str] = []
    monkeypatch.setattr(
        "app.services.developer_apps.credentials_for_account",
        lambda *_args, **_kwargs: DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1, app_name="pytest"),
    )
    monkeypatch.setattr(
        "app.services.material_cache.gateway.cache_material_source",
        lambda *args, **kwargs: calls.append(args[1]) or SendResult(True, remote_message_id="material-601"),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="缓存号", phone_masked="101", status=AccountStatus.ACTIVE.value, session_ciphertext="session"))
        session.add(
            Material(
                id=9002,
                tenant_id=1,
                title="FloodWait 图片",
                material_type="图片",
                content="https://trusted.example.com/flood.png",
                cache_ready_status="flood_wait",
                last_cache_flood_wait_until=_now() + timedelta(minutes=5),
            )
        )
        session.commit()

    def factory():
        return Session(engine)

    assert drain_material_cache(factory, limit=10) == 0
    assert calls == []

    with Session(engine) as session:
        material = session.get(Material, 9002)
        material.last_cache_flood_wait_until = _now() - timedelta(minutes=1)
        session.commit()

    assert drain_material_cache(factory, limit=10) == 1
    assert calls == ["https://trusted.example.com/flood.png"]

    get_settings.cache_clear()


def test_material_create_rejects_public_ready_spoof_and_unsafe_url():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        material = create_material(
            session,
            MaterialCreate(
                tenant_id=1,
                title="伪造 ready",
                material_type="图片",
                content="https://trusted.example.com/a.png",
                cache_ready_status="ready",
                tg_cache_peer_id="fake-peer",
                tg_cache_message_id="fake-id",
            ),
            "tester",
        )
        assert material.cache_ready_status == "not_cached"
        assert material.tg_cache_peer_id == ""
        assert material.tg_cache_message_id == ""

        try:
            create_material(
                session,
                MaterialCreate(tenant_id=1, title="内网图", material_type="图片", content="https://127.0.0.1/a.png"),
                "tester",
            )
        except ValueError as exc:
            assert "内网" in str(exc) or "localhost" in str(exc)
        else:
            raise AssertionError("unsafe material url should be rejected")


def test_material_url_deep_probe_checks_dns_redirect_type_and_size(monkeypatch):
    monkeypatch.setenv("MATERIAL_MAX_BYTES", "32")
    get_settings.cache_clear()

    def public_resolver(host, port, type=None):  # noqa: ANN001 - mirrors socket.getaddrinfo.
        if host == "private.example":
            return [(None, None, None, "", ("10.0.0.5", port))]
        return [(None, None, None, "", ("93.184.216.34", port))]

    class Response:
        status = 200

        def __init__(self, headers):
            self.headers = headers

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Opener:
        def __init__(self, headers):
            self.headers = headers

        def open(self, _request, timeout=0):  # noqa: ANN001 - mirrors urllib opener.
            return Response(self.headers)

    class RedirectOpener:
        def __init__(self):
            self.calls = 0

        def open(self, request, timeout=0):  # noqa: ANN001 - mirrors urllib opener.
            self.calls += 1
            if self.calls == 1:
                raise __import__("urllib.error").error.HTTPError(request.full_url, 302, "Found", {"Location": "https://cdn.example/final.png"}, None)
            return Response({"Content-Type": "image/png", "Content-Length": "12"})

    assert deep_probe_material_url(
        "https://cdn.example/a.png",
        material_type="图片",
        resolver=public_resolver,
        opener=Opener({"Content-Type": "image/png", "Content-Length": "12"}),
    ) == "https://cdn.example/a.png"
    assert deep_probe_material_url("https://cdn.example/redirect.png", material_type="图片", resolver=public_resolver, opener=RedirectOpener()) == "https://cdn.example/final.png"
    try:
        deep_probe_material_url("https://private.example/a.png", material_type="图片", resolver=public_resolver, opener=Opener({"Content-Type": "image/png"}))
    except ValueError as exc:
        assert "内网" in str(exc)
    else:
        raise AssertionError("private DNS target should be rejected")
    try:
        deep_probe_material_url("https://cdn.example/a.png", material_type="图片", resolver=public_resolver, opener=Opener({"Content-Type": "text/html"}))
    except ValueError as exc:
        assert "Content-Type" in str(exc)
    else:
        raise AssertionError("bad content type should be rejected")
    try:
        deep_probe_material_url("https://cdn.example/a.png", material_type="图片", resolver=public_resolver, opener=Opener({"Content-Type": "image/png", "Content-Length": "64"}))
    except ValueError as exc:
        assert "过大" in str(exc)
    else:
        raise AssertionError("oversized content length should be rejected")

    get_settings.cache_clear()


def test_material_update_content_change_clears_cache_refs():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        material = Material(
            id=9101,
            tenant_id=1,
            title="已缓存图片",
            material_type="图片",
            content="https://trusted.example.com/old.png",
            cache_ready_status="ready",
            tg_cache_peer_id="cache-peer",
            tg_cache_message_id="old-id",
            asset_version_id=2,
            tg_ref_version_id=3,
        )
        session.add(material)
        session.commit()
        updated = update_material(
            session,
            9101,
            MaterialUpdate(content="https://trusted.example.com/new.png", cache_ready_status="ready", tg_cache_peer_id="fake", tg_cache_message_id="fake"),
            "tester",
        )

    assert updated.content == "https://trusted.example.com/new.png"
    assert updated.cache_ready_status == "not_cached"
    assert updated.tg_cache_peer_id == ""
    assert updated.tg_cache_message_id == ""
    assert updated.asset_version_id == 3
    assert updated.tg_ref_version_id == 4


def test_material_center_reference_summary_disable_and_restore():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        material = Material(
            id=9110,
            tenant_id=1,
            title="引用保护图片",
            material_type="图片",
            content="https://trusted.example.com/ref.png",
            tags="头像包,活动图",
            review_status="已审核",
            cache_ready_status="ready",
        )
        session.add(material)
        session.add(
            MessageTask(
                tenant_id=1,
                content="引用图片",
                message_type="图片",
                material_id=9110,
                idempotency_key="material-reference-task",
            )
        )
        session.add(
            Action(
                id="material-reference-action",
                tenant_id=1,
                task_id="task-material-reference",
                task_type="group_relay",
                action_type="send_message",
                payload={"media_segments": [{"material_id": 9110}]},
            )
        )
        session.add(
            RuleSetVersion(
                tenant_id=1,
                rule_set_id=1,
                version=1,
                status="published",
                routing={"material_policy": {"material_id": 9110}},
            )
        )
        session.add(
            OperationPlanTemplate(
                tenant_id=1,
                name="素材运营方案",
                strategy_config={"material_id": 9110},
                task_blueprints=[],
            )
        )
        session.add(
            TgAccountSecurityBatchItem(
                batch_id=1,
                tenant_id=1,
                account_id=101,
                avatar_source="material:9110",
            )
        )
        session.commit()

        listed = list_materials(session, 1)[0]
        summary = listed.reference_summary
        disabled = disable_material(session, 9110, "tester", reason="素材被投诉，先下线")
        disabled_status = disabled.review_status
        disabled_total = disabled.reference_summary.total_count
        restored = restore_material(session, 9110, "tester")

    assert summary.message_task_count == 1
    assert summary.action_count == 1
    assert summary.rule_version_count == 1
    assert summary.operation_plan_count == 1
    assert summary.account_profile_batch_count == 1
    assert summary.total_count == 5
    assert disabled_status == "已禁用"
    assert disabled_total == 5
    assert restored.review_status == "已审核"
    assert restored.reference_summary.total_count == 5


def test_material_list_uses_batch_reference_summary(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    def fail_single_material_summary(*_args, **_kwargs):
        raise AssertionError("list_materials should not compute references material-by-material")

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                Material(id=9201, tenant_id=1, title="素材一", material_type="图片", content="https://example.local/1.png"),
                Material(id=9202, tenant_id=1, title="素材二", material_type="图片", content="https://example.local/2.png"),
            ]
        )
        session.commit()

        monkeypatch.setattr(ai_config_service, "material_reference_summary", fail_single_material_summary)
        listed = list_materials(session, 1)

    assert [item.id for item in listed] == [9202, 9201]
    assert [item.reference_summary.total_count for item in listed] == [0, 0]


def test_material_asset_and_tg_ref_versions_are_recorded(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("MATERIAL_CACHE_PEER_ID", "material-cache-peer")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.services.developer_apps.credentials_for_account",
        lambda *_args, **_kwargs: DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1, app_name="pytest"),
    )
    monkeypatch.setattr(
        "app.services.material_cache.gateway.cache_material_source",
        lambda *args, **kwargs: SendResult(True, remote_message_id="version-801"),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="缓存号", phone_masked="101", status=AccountStatus.ACTIVE.value, session_ciphertext="session"))
        material = create_material(
            session,
            MaterialCreate(tenant_id=1, title="版本图片", material_type="图片", content="https://trusted.example.com/v1.png"),
            "tester",
        )
        material_id = material.id
        assert session.scalar(select(MaterialAssetVersion).where(MaterialAssetVersion.material_id == material_id, MaterialAssetVersion.asset_version_id == 1))
        assert session.scalar(select(MaterialTgRefVersion).where(MaterialTgRefVersion.material_id == material_id, MaterialTgRefVersion.tg_ref_version_id == 1)).cache_status == "not_cached"
        update_material(session, material_id, MaterialUpdate(content="https://trusted.example.com/v2.png"), "tester")
        assert session.scalar(select(MaterialAssetVersion).where(MaterialAssetVersion.material_id == material_id, MaterialAssetVersion.asset_version_id == 2)).content.endswith("/v2.png")
        assert session.scalar(select(MaterialTgRefVersion).where(MaterialTgRefVersion.material_id == material_id, MaterialTgRefVersion.tg_ref_version_id == 2)).cache_status == "not_cached"
        session.commit()

    def factory():
        return Session(engine)

    assert drain_material_cache(factory, limit=10) == 1

    with Session(engine) as session:
        material = session.get(Material, material_id)
        ref = session.scalar(select(MaterialTgRefVersion).where(MaterialTgRefVersion.material_id == material_id, MaterialTgRefVersion.tg_ref_version_id == material.tg_ref_version_id))
        assert material.cache_ready_status == "ready"
        assert ref.cache_status == "ready"
        assert ref.tg_cache_message_id == "version-801"

    get_settings.cache_clear()


def test_custom_emoji_material_is_ready_and_builds_custom_segment():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        material = create_material(
            session,
            MaterialCreate(
                tenant_id=1,
                title="自定义表情",
                material_type="表情包",
                content="custom_emoji:1234567890:🙂",
                emoji_asset_kind="custom_emoji",
            ),
            "tester",
        )
        task = MessageTask(
            tenant_id=1,
            content="",
            message_type="表情包",
            material_id=material.id,
            idempotency_key="custom-emoji-test",
        )
        session.add(task)
        session.commit()

        refreshed = session.get(Material, material.id)
        ref = session.scalar(select(MaterialTgRefVersion).where(MaterialTgRefVersion.material_id == material.id))
        segments = build_outbound_segments(session, task)

    assert refreshed.cache_ready_status == "ready"
    assert refreshed.tg_cache_peer_id == ""
    assert ref.cache_status == "ready"
    assert segments[0].segment_type == "表情包"
    assert segments[0].source == "custom_emoji:1234567890:🙂"
    assert TelethonTelegramGateway._parse_custom_emoji_source(segments[0].source) == (1234567890, "🙂")
    assert TelethonTelegramGateway._telegram_entity_length("🙂") == 2


def test_custom_emoji_runtime_error_maps_to_unavailable_reason():
    result = TelethonTelegramGateway._map_send_error(RuntimeError("MessageEntityCustomEmoji document id is unavailable"))

    assert result.ok is False
    assert result.failure_type == "custom_emoji_unavailable"


def test_discussion_message_id_error_maps_to_comment_unavailable():
    result = TelethonTelegramGateway._map_send_error(
        RuntimeError("The message ID used in the peer was invalid (caused by GetDiscussionMessageRequest)")
    )

    assert result.ok is False
    assert result.failure_type == FailureType.COMMENT_UNAVAILABLE.value
    assert "消息ID属于频道帖子" in (result.detail or "")


def test_uploaded_material_temp_file_is_removed_after_cache_success(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    monkeypatch.setenv("MATERIAL_CACHE_PEER_ID", "material-cache-peer")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.services.developer_apps.credentials_for_account",
        lambda *_args, **_kwargs: DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1, app_name="pytest"),
    )
    monkeypatch.setattr(
        "app.services.material_cache.gateway.cache_material_source",
        lambda *args, **kwargs: SendResult(True, remote_message_id="upload-701"),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="缓存号", phone_masked="101", status=AccountStatus.ACTIVE.value, session_ciphertext="session"))
        material = create_uploaded_material(
            session,
            tenant_id=1,
            title="上传图片",
            material_type="图片",
            tags="上传",
            caption="caption",
            filename="hello.png",
            content_type="image/png",
            data=b"png-bytes",
            actor="tester",
        )
        temp_path = material.content
        material_id = material.id
        assert temp_path.startswith(str(tmp_path))
        assert Path(temp_path).exists()

    def factory():
        return Session(engine)

    assert drain_material_cache(factory, limit=10) == 1
    assert not Path(temp_path).exists()

    with Session(engine) as session:
        material = session.get(Material, material_id)
        asset_version = session.scalar(select(MaterialAssetVersion).where(MaterialAssetVersion.material_id == material_id))
        assert material.cache_ready_status == "ready"
        assert material.tg_cache_message_id == "upload-701"
        assert material.content == ""
        assert asset_version.content == ""

    get_settings.cache_clear()


def test_material_cache_health_summarizes_material_and_source_queues(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("MATERIAL_CACHE_PEER_ID", "material-cache-peer")
    monkeypatch.setenv("SOURCE_MEDIA_CACHE_PEER_ID", "source-cache-peer")
    get_settings.cache_clear()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="缓存号", phone_masked="101", status=AccountStatus.ACTIVE.value, session_ciphertext="session"))
        session.add(Material(id=9201, tenant_id=1, title="待缓存", material_type="图片", content="https://trusted.example.com/a.png", cache_ready_status="not_cached"))
        session.add(Material(id=9202, tenant_id=1, title="失败素材", material_type="图片", content="https://trusted.example.com/b.png", cache_ready_status="cache_failed", last_cache_error="cache_failed"))
        task = Task(id="health-task", tenant_id=1, name="等待缓存", type="group_relay", status="running")
        action = Action(id="health-action", tenant_id=1, task_id=task.id, task_type=task.type, action_type="send_message", status=WAITING_MATERIAL_CACHE)
        source_asset = SourceMediaAsset(id="health-asset", tenant_id=1, source_message_id="src", cache_status="cache_failed", failure_reason="source_deleted")
        session.add_all([task, action, source_asset])
        session.commit()

        health = material_cache_health(session, 1)

    assert health.material_cache_peer_configured is True
    assert health.source_media_cache_peer_configured is True
    assert health.active_cache_account_count == 1
    assert {item.status: item.count for item in health.material_status_counts}["not_cached"] == 1
    assert health.cache_failed_count == 2
    assert health.waiting_action_count == 1
    assert {item.scope for item in health.recent_errors} == {"material", "source_media"}

    get_settings.cache_clear()


def test_temp_file_cleanup_removes_only_expired_platform_temp_files(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    expired = temp_dir("material-tmp") / "expired.bin"
    fresh = temp_dir("material-tmp") / "fresh.bin"
    avatar_dir = tmp_path / "avatars" / "1" / "1"
    avatar_dir.mkdir(parents=True)
    avatar = avatar_dir / "keep.png"
    expired.write_bytes(b"expired")
    fresh.write_bytes(b"fresh")
    avatar.write_bytes(b"avatar")
    now_ts = _now().timestamp()
    utime(expired, (now_ts - TEMP_FILE_TTL_SECONDS - 60, now_ts - TEMP_FILE_TTL_SECONDS - 60))
    utime(fresh, (now_ts, now_ts))
    utime(avatar, (now_ts - TEMP_FILE_TTL_SECONDS - 60, now_ts - TEMP_FILE_TTL_SECONDS - 60))

    assert cleanup_temp_files(now_ts=now_ts) == 1
    assert not expired.exists()
    assert fresh.exists()
    assert avatar.exists()

    get_settings.cache_clear()


def test_published_rule_version_is_immutable_by_new_draft_flow():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        rule_set = create_rule_set(session, 1, RuleSetCreate(name="转发规则", task_types=["group_relay"]), "tester")
        draft = create_rule_set_version(
            session,
            1,
            rule_set.id,
            RuleSetVersionCreate(version_note="收紧输出校验", output_checks={"forbid_links": True}),
            "tester",
        )
        draft_version = next(version for version in draft.versions if version.status == "draft")
        published = publish_rule_set_version(session, 1, rule_set.id, draft_version.id, "tester")

    statuses = {version.version: version.status for version in published.versions}
    assert statuses == {2: "published", 1: "archived"}
    assert published.active_version_id == draft_version.id


def test_output_policy_transforms_once_then_drops_if_still_invalid():
    result = apply_output_policy(
        "请联系 @someone 看链接 https://example.com",
        {"forbid_mentions": True, "forbid_links": True, "failure_strategy": "transform_once_drop"},
        {"remove_mentions": True},
    )

    assert result.allowed is False
    assert result.action == "drop"
    assert result.reason == "命中链接规则"


def test_transform_content_removes_configured_keywords_case_insensitively():
    assert transform_content("这是VIP内容，也是vip内容", {"delete_keywords": ["VIP"]}) == "这是内容，也是内容"


def test_rule_version_copy_and_rollback_create_traceable_versions():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        rule_set = create_rule_set(
            session,
            1,
            RuleSetCreate(name="版本治理", filters={"keyword_whitelist": ["公告"]}, output_checks={"forbid_links": False}),
            "tester",
        )
        copied = copy_rule_set_version(session, 1, rule_set.id, rule_set.active_version_id, "tester", reason="基于当前发布版本调整")
        copied_version = next(version for version in copied.versions if version.version == 2)
        assert copied_version.status == "draft"
        assert copied_version.version_note == "复制自 v1"
        assert copied_version.filters == {"keyword_whitelist": ["公告"]}

        tightened = create_rule_set_version(
            session,
            1,
            rule_set.id,
            RuleSetVersionCreate(version_note="收紧", filters={"keyword_whitelist": ["活动"]}, output_checks={"forbid_links": True}),
            "tester",
        )
        draft = next(version for version in tightened.versions if version.version == 3)
        published = publish_rule_set_version(session, 1, rule_set.id, draft.id, "tester", reason="活动白名单收紧")
        assert next(version for version in published.versions if version.version == 3).status == "published"

        rolled_back = rollback_rule_set_version(session, 1, rule_set.id, rule_set.active_version_id, "tester", reason="活动规则异常回退")
        active = next(version for version in rolled_back.versions if version.id == rolled_back.active_version_id)
        audit_details = [row.detail for row in session.query(AuditLog).filter(AuditLog.target_id == str(rule_set.id)).order_by(AuditLog.id.asc())]

    assert active.version == 4
    assert active.status == "published"
    assert active.version_note == "回滚自 v1"
    assert active.filters == {"keyword_whitelist": ["公告"]}
    assert any("reason=基于当前发布版本调整" in detail and "diff=none" in detail for detail in audit_details)
    assert any("reason=活动白名单收紧" in detail and "diff=filters,output_checks" in detail for detail in audit_details)
    assert any("reason=活动规则异常回退" in detail and "diff=filters,output_checks" in detail for detail in audit_details)


def test_rule_set_config_publish_audit_records_reason_and_diff():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        rule_set = create_rule_set(
            session,
            1,
            RuleSetCreate(name="配置发布治理", filters={"keyword_whitelist": ["公告"]}, output_checks={"forbid_links": False}),
            "tester",
        )
        updated = update_rule_set_config(
            session,
            1,
            rule_set.id,
            RuleSetVersionCreate(
                version_note="收紧链接",
                publish_reason="链接风险上升",
                filters={"keyword_whitelist": ["公告"]},
                output_checks={"forbid_links": True},
            ),
            "tester",
        )
        active = next(version for version in updated.versions if version.id == updated.active_version_id)
        audit_detail = session.query(AuditLog.detail).filter(AuditLog.target_id == str(rule_set.id), AuditLog.action == "更新规则集配置并发布").order_by(AuditLog.id.desc()).scalar()

    assert active.status == "published"
    assert active.version == 2
    assert "reason=链接风险上升" in audit_detail
    assert "diff=output_checks" in audit_detail


def test_bound_tasks_show_fixed_and_follow_current_resolution():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        rule_set = create_rule_set(session, 1, RuleSetCreate(name="任务绑定规则"), "tester")
        session.add_all(
            [
                Task(tenant_id=1, name="跟随当前发布", type="group_relay", status="active", type_config={"rule_set_id": rule_set.id}),
                Task(tenant_id=1, name="固定版本", type="group_ai_chat", status="active", type_config={"rule_set_version_id": rule_set.active_version_id}),
                Task(tenant_id=1, name="未绑定", type="message_send", status="active", type_config={}),
            ]
        )
        session.commit()
        rows = list_rule_set_bound_tasks(session, 1, rule_set.id)

    rows_by_name = {row.name: row for row in rows}
    assert set(rows_by_name) == {"跟随当前发布", "固定版本"}
    assert rows_by_name["跟随当前发布"].binding_mode == "follow_current"
    assert rows_by_name["跟随当前发布"].resolved_rule_set_version_id == rule_set.active_version_id
    assert rows_by_name["固定版本"].binding_mode == "fixed_version"
    assert rows_by_name["固定版本"].resolved_rule_set_version_id == rule_set.active_version_id


def test_follow_current_relay_config_solidifies_active_version_for_execution_item():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        rule_set = create_rule_set(session, 1, RuleSetCreate(name="执行固化", task_types=["group_relay"]), "tester")
        task = Task(tenant_id=1, name="监听转发", type="group_relay", status="active", type_config={"rule_set_id": rule_set.id})
        session.add(task)
        session.commit()

        config = effective_relay_config(session, task)
        assert config["rule_binding_mode"] == "follow_current"
        assert config["resolved_rule_set_version_id"] == rule_set.active_version_id

        draft = create_rule_set_version(session, 1, rule_set.id, RuleSetVersionCreate(filters={"keyword_whitelist": ["新规则"]}), "tester")
        draft_version = next(version for version in draft.versions if version.status == "draft")
        published = publish_rule_set_version(session, 1, rule_set.id, draft_version.id, "tester")
        next_config = effective_relay_config(session, task)

    assert published.active_version_id != config["resolved_rule_set_version_id"]
    assert next_config["resolved_rule_set_version_id"] == published.active_version_id


def test_legacy_keyword_rules_do_not_feed_new_rule_center_or_tester():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(ContentKeywordRule(tenant_id=1, keyword="旧口径", match_type="contains", is_active=True))
        session.commit()

        summary = rule_center_summary(session, 1)
        result = preview_rules(session, 1, "这条消息包含旧口径")

    assert summary.keyword_rule_count == 0
    assert summary.keyword_metrics == []
    assert all(item.source != "keyword" for item in summary.items)
    assert result.should_block is False
    assert result.hits == []
    assert result.result == "未命中规则条件"


def test_draft_rule_version_cannot_be_resolved_for_real_task_execution():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        rule_set = create_rule_set(session, 1, RuleSetCreate(name="草稿隔离"), "tester")
        draft = create_rule_set_version(session, 1, rule_set.id, RuleSetVersionCreate(filters={"keyword_whitelist": ["草稿"]}), "tester")
        draft_version = next(version for version in draft.versions if version.status == "draft")
        task = Task(tenant_id=1, name="错误绑定草稿", type="group_ai_chat", status="running", type_config={"rule_set_version_id": draft_version.id})
        session.add(task)
        session.commit()

        resolved = bound_rule_version(session, task)

    assert resolved is None
    assert task.last_error == "绑定的规则版本尚未发布"
