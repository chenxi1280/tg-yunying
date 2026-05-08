from __future__ import annotations

from collections import Counter
from datetime import timedelta
import random
from uuid import uuid4

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.gateways import GroupMessageSnapshot
from app.main import app
from app.models import AiDraft, AiUsageLedger, AppUser, Campaign, CampaignProcessedMessage, ContentKeywordRule, GroupContextMessage, MessageTask, TaskStatus, TgGroup
from app.services.campaign_runs import build_participation_plan, process_continuous_campaign
from app.services.content_filters import filter_outbound_content
from app.worker import drain_once
from app.models.enums import now
from tests.test_workflow import auth_headers, ensure_test_workspace


def _future_iso(minutes: int = 60) -> str:
    return (now() + timedelta(minutes=minutes)).isoformat()


def _expire_tenant_subscription(tenant_id: int = 1) -> tuple[int, str, object, object]:
    with SessionLocal() as session:
        user = session.query(AppUser).filter(AppUser.tenant_id == tenant_id, AppUser.role == "普通用户").order_by(AppUser.id.asc()).first()
        assert user is not None
        original = (user.id, user.subscription_status, user.subscription_started_at, user.subscription_expires_at)
        user.subscription_status = "expired"
        user.subscription_expires_at = now() - timedelta(days=1)
        session.commit()
        return original


def _restore_tenant_subscription(original: tuple[int, str, object, object]) -> None:
    user_id, status, started_at, expires_at = original
    with SessionLocal() as session:
        user = session.get(AppUser, user_id)
        assert user is not None
        user.subscription_status = status
        user.subscription_started_at = started_at
        user.subscription_expires_at = expires_at
        session.commit()


def test_participation_plan_keeps_ratio_and_max_per_account():
    plan = build_participation_plan(
        list(range(1, 51)),
        50,
        ratio=0.8,
        max_messages_per_account=2,
        rng=random.Random(7),
    )

    counts = Counter(plan)
    assert len(plan) == 50
    assert len(counts) == 40
    assert max(counts.values()) <= 2


def test_ai_activity_campaign_auto_approves_and_queues_tasks():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)

        campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": group["id"],
                "title": "持续 AI 活跃",
                "campaign_type": "AI 活跃",
                "execution_mode": "ai_activity",
                "topic": "围绕功能体验自然暖群",
                "target_group_ids": [group["id"]],
                "selected_account_ids_by_group": {str(group["id"]): [account["id"]]},
                "ends_at": _future_iso(),
                "run_interval_seconds": 1,
                "max_ai_tokens": 100000,
                "jitter_min_seconds": 0,
                "jitter_max_seconds": 0,
                "batch_interval_seconds": 0,
                "respect_send_window": False,
            },
        )
        assert campaign.status_code == 200, campaign.text
        assert campaign.json()["status"] == TaskStatus.QUEUED.value

        assert drain_once() >= 1

        with SessionLocal() as session:
            db_campaign = session.get(Campaign, campaign.json()["id"])
            drafts = session.query(AiDraft).filter_by(campaign_id=db_campaign.id).all()
            tasks = session.query(MessageTask).filter_by(campaign_id=db_campaign.id).all()
            assert drafts
            assert all(draft.status == TaskStatus.APPROVED.value for draft in drafts)
            assert tasks
            assert tasks[0].status == TaskStatus.QUEUED.value


def test_ai_activity_campaign_stops_when_token_limit_is_reached():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)

        campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": group["id"],
                "title": "Token 上限任务",
                "campaign_type": "AI 活跃",
                "execution_mode": "ai_activity",
                "topic": "达到上限后停止",
                "target_group_ids": [group["id"]],
                "selected_account_ids_by_group": {str(group["id"]): [account["id"]]},
                "ends_at": _future_iso(),
                "run_interval_seconds": 1,
                "max_ai_tokens": 5,
            },
        ).json()

        with SessionLocal() as session:
            user_id = session.query(AppUser.id).filter(AppUser.tenant_id == 1).order_by(AppUser.id.asc()).first()[0]
            session.add(
                AiUsageLedger(
                    tenant_id=1,
                    user_id=user_id,
                    campaign_id=campaign["id"],
                    group_id=group["id"],
                    total_tokens=10,
                    request_status="success",
                )
            )
            session.commit()

        assert drain_once() >= 0

        with SessionLocal() as session:
            db_campaign = session.get(Campaign, campaign["id"])
            assert db_campaign.status == TaskStatus.COMPLETED.value
            assert db_campaign.used_ai_tokens == 10


def test_ai_activity_campaign_pauses_when_subscription_expired():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)

        campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": group["id"],
                "title": "过期订阅持续 AI 活跃",
                "campaign_type": "AI 活跃",
                "execution_mode": "ai_activity",
                "topic": "订阅过期不应继续发送",
                "target_group_ids": [group["id"]],
                "selected_account_ids_by_group": {str(group["id"]): [account["id"]]},
                "ends_at": _future_iso(),
                "run_interval_seconds": 1,
            },
        ).json()

        original = _expire_tenant_subscription()
        try:
            with SessionLocal() as session:
                assert process_continuous_campaign(session, campaign["id"]) == 0

            with SessionLocal() as session:
                db_campaign = session.get(Campaign, campaign["id"])
                assert db_campaign.status == TaskStatus.PAUSED.value
                assert db_campaign.last_error == "subscription inactive"
                assert session.query(AiDraft).filter_by(campaign_id=campaign["id"]).count() == 0
                assert session.query(MessageTask).filter_by(campaign_id=campaign["id"]).count() == 0
        finally:
            _restore_tenant_subscription(original)


def test_mirror_forward_campaign_pauses_when_subscription_expired():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, target_group = ensure_test_workspace(client, headers)

        response = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": target_group["id"],
                "title": "过期订阅监听转发",
                "campaign_type": "监听转发",
                "execution_mode": "mirror_forward",
                "topic": "订阅过期不应转发",
                "source_group_ids": [target_group["id"]],
                "target_group_ids": [target_group["id"]],
                "selected_account_ids_by_group": {str(target_group["id"]): [account["id"]]},
                "ends_at": _future_iso(),
                "run_interval_seconds": 1,
            },
        )
        assert response.status_code == 200, response.text
        campaign = response.json()

        original = _expire_tenant_subscription()
        try:
            with SessionLocal() as session:
                assert process_continuous_campaign(session, campaign["id"]) == 0

            with SessionLocal() as session:
                db_campaign = session.get(Campaign, campaign["id"])
                assert db_campaign.status == TaskStatus.PAUSED.value
                assert db_campaign.last_error == "subscription inactive"
                assert session.query(AiDraft).filter_by(campaign_id=campaign["id"]).count() == 0
                assert session.query(MessageTask).filter_by(campaign_id=campaign["id"]).count() == 0
        finally:
            _restore_tenant_subscription(original)


def test_mirror_forward_multi_target_deduplicates_messages(monkeypatch):
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _ = ensure_test_workspace(client, headers)
        groups = client.get("/api/groups", headers=headers).json()
        source_groups = groups[:1]
        target_groups = groups[1:3]
        assert len(target_groups) == 2
        with SessionLocal() as session:
            session.query(GroupContextMessage).filter(GroupContextMessage.group_id.in_([item["id"] for item in source_groups])).delete(synchronize_session=False)
            session.commit()
        for group in [*source_groups, *target_groups]:
            client.post(f"/api/groups/{group['id']}/authorize", headers=headers, json={"auth_status": "已授权运营"})
        for group in source_groups:
            client.patch(
                f"/api/groups/{group['id']}",
                headers=headers,
                json={"listener_enabled": True, "listener_account_ids": [account["id"]]},
            )

        def fake_fetch(*args, **kwargs):
            peer_id = args[1]
            return [
                GroupMessageSnapshot(
                    remote_message_id=f"remote-{peer_id}",
                    sender_peer_id=f"real-{uuid4().hex[:6]}",
                    sender_name="真人",
                    content=f"{peer_id} 今天气氛不错",
                )
            ]

        monkeypatch.setattr("app.services.group_listeners.gateway.fetch_group_messages", fake_fetch)

        response = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": target_groups[0]["id"],
                "title": "多源监听转发",
                "campaign_type": "监听转发",
                "execution_mode": "mirror_forward",
                "topic": "同步源群气氛",
                "source_group_ids": [item["id"] for item in source_groups],
                "target_group_ids": [item["id"] for item in target_groups],
                "selected_account_ids_by_group": {str(group["id"]): [account["id"]] for group in target_groups},
                "ends_at": _future_iso(),
                "run_interval_seconds": 1,
                "jitter_min_seconds": 0,
                "jitter_max_seconds": 0,
                "batch_interval_seconds": 0,
                "respect_send_window": False,
            },
        )
        assert response.status_code == 200, response.text
        campaign_id = response.json()["id"]

        with SessionLocal() as session:
            first = process_continuous_campaign(session, campaign_id)
        with SessionLocal() as session:
            second = process_continuous_campaign(session, campaign_id)
            task_count = session.query(MessageTask).filter_by(campaign_id=campaign_id).count()
            processed_count = session.query(CampaignProcessedMessage).filter_by(campaign_id=campaign_id).count()

        expected = len(source_groups) * len(target_groups)
        assert first == expected
        assert second == 0
        assert task_count == expected
        assert processed_count == expected


def test_content_filter_rejects_mentions_replies_tenant_keywords_and_group_rules():
    with TestClient(app) as client:
        headers = auth_headers(client)
        _, group = ensure_test_workspace(client, headers)

        with SessionLocal() as session:
            db_group = session.get(TgGroup, group["id"])
            db_group.banned_words = "群禁词"
            db_group.link_whitelist = "allowed.example"
            session.add(ContentKeywordRule(tenant_id=1, keyword="租户禁词", match_type="contains", is_active=True))
            session.commit()

            assert not filter_outbound_content(session, tenant_id=1, group=db_group, content="@someone hello", reject_mentions=True).ok
            assert not filter_outbound_content(session, tenant_id=1, group=db_group, content="回复 Bob: hello", reject_replies=True).ok
            assert "租户关键词" in filter_outbound_content(session, tenant_id=1, group=db_group, content="这里有租户禁词").reason
            assert "群禁词" in filter_outbound_content(session, tenant_id=1, group=db_group, content="这里有群禁词").reason
            assert "链接不在白名单" in filter_outbound_content(session, tenant_id=1, group=db_group, content="看 http://bad.example").reason
            assert filter_outbound_content(session, tenant_id=1, group=db_group, content="看 http://allowed.example/page").ok
