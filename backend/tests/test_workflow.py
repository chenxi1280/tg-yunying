from datetime import UTC, datetime, timedelta
from io import BytesIO
import json
from uuid import uuid4
from zipfile import ZipFile

from app.ai_gateway import AiDraftCandidate, AiGenerationResult, AiUsage, mock_candidates
from app.config import get_settings
from app.auth import get_challenge_target
from app.database import SessionLocal
from app.main import app
from app.integrations.telegram import ChannelCommentSnapshot, ChannelMessageSnapshot, DeveloperAppCredentials, GroupMessageSnapshot, GroupSnapshot, OperationResult, SendResult
from app.models import AccountStatus, Action, AiDraft, AiUsageLedger, AuditLog, Campaign, DeveloperAppHealthStatus, FailureType, GroupContextMessage, ListenerSourceState, ManualOperationRecord, Material, MessageFingerprint, MessageTask, OperationTarget, OperationTaskAttempt, ReviewQueue, SchedulingSetting, SourceMediaAsset, Task, TaskStatus, TelegramDeveloperApp, Tenant, TgAccount, TgAccountOnlineState, TgAccountProfileSyncRecord, TgAccountSyncRecord, TgGroup, TgGroupAccount, TgLoginFlow, VerificationTask
from app.services._common import _now
from app.services.notifications import NotificationResult
from app.services.task_center.listener_runtime import reset_listener_runtime_cache
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import inspect, select
from tests.ai_group_voice_profile_fixtures import assume_default_ai_group_voice_profiles


_workspace_phone_suffix = 1000


@pytest.fixture(autouse=True)
def assume_group_ai_voice_profiles_for_workflow_tests(monkeypatch):
    assume_default_ai_group_voice_profiles(monkeypatch)


def workflow_ai_active_pacing() -> dict:
    return {
        "mode": "fixed",
        "interval_seconds_min": 0,
        "interval_seconds_max": 0,
        "jitter_percent": 0,
        "operation_profile": {
            "template_id": "pytest_always_active",
            "source": "manual",
            "hourly_activity_curve": [10] * 24,
            "quiet_threshold": 2,
            "peak_threshold": 8,
            "manual_override": True,
        },
    }


def _workflow_ai_token(index: int) -> str:
    return f"身边例子{index:03d}{uuid4().hex[:6]}"


def _next_test_phone(prefix: str = "+8613800") -> str:
    global _workspace_phone_suffix
    from app.services.accounts import mask_phone

    for _ in range(10000):
        _workspace_phone_suffix += 1
        phone = f"{prefix}{_workspace_phone_suffix:04d}"
        with SessionLocal() as session:
            exists = session.query(TgAccount.id).filter(TgAccount.phone_masked == mask_phone(phone)).first()
        if not exists:
            return phone
    raise AssertionError("没有可用的测试手机号后缀")


def skip_legacy_task_center_flow() -> None:
    pytest.skip("旧 Campaign/Operation 任务中心已下线，由 5 类型 task_center 测试覆盖")


def task_detail_actions(client: TestClient, headers: dict[str, str], task_id: str, action_type: str | None = None) -> list[dict]:
    params = "page=1&page_size=200"
    if action_type:
        params = f"{params}&action_type={action_type}"
    response = client.get(f"/api/tasks/{task_id}/actions?{params}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def compact_task_debug(task: dict) -> dict:
    return {
        "status": task.get("status"),
        "next_run_at": task.get("next_run_at"),
        "last_error": task.get("last_error"),
        "stats": task.get("stats"),
    }


def compact_action_debug(actions: list[dict]) -> list[dict]:
    return [
        {
            "type": item.get("action_type"),
            "status": item.get("status"),
            "scheduled_at": item.get("scheduled_at"),
            "executed_at": item.get("executed_at"),
            "result": item.get("result"),
            "id": item.get("id"),
            "memory_id": (item.get("payload") or {}).get("ai_message_memory_id"),
            "cycle_id": (item.get("payload") or {}).get("cycle_id"),
            "message": (item.get("payload") or {}).get("message_text"),
        }
        for item in actions
    ]


def actions_for_cycle_suffix(actions: list[dict], suffix: str) -> list[dict]:
    return [
        action
        for action in actions
        if str((action.get("payload") or {}).get("cycle_id") or "").endswith(suffix)
    ]


def wait_for_cycle_success(
    client: TestClient,
    headers: dict[str, str],
    task_id: str,
    suffix: str,
    attempts: int = 6,
) -> tuple[dict, list[dict]]:
    from app.services.task_center.service import drain_task_center

    detail: dict = {}
    actions: list[dict] = []
    for _ in range(attempts):
        make_task_send_actions_due(task_id)
        drain_task_center(SessionLocal, 20)
        detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        actions = task_detail_actions(client, headers, task_id)
        cycle_actions = actions_for_cycle_suffix(actions, suffix)
        if any(action["status"] == "success" for action in cycle_actions):
            return detail, cycle_actions
    return detail, actions_for_cycle_suffix(actions, suffix)


def wait_for_new_success_actions(
    client: TestClient,
    headers: dict[str, str],
    task_id: str,
    known_action_ids: set[str],
    attempts: int = 6,
) -> tuple[dict, list[dict]]:
    from app.services.task_center.service import drain_task_center

    detail: dict = {}
    actions: list[dict] = []
    for _ in range(attempts):
        make_task_send_actions_due(task_id)
        drain_task_center(SessionLocal, 20)
        detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        actions = task_detail_actions(client, headers, task_id)
        new_success = [
            action
            for action in actions
            if str(action.get("id") or "") not in known_action_ids and action.get("status") == "success"
        ]
        if new_success:
            return detail, new_success
    return detail, [
        action
        for action in actions
        if str(action.get("id") or "") not in known_action_ids
    ]


def make_task_send_actions_due(task_id: str) -> int:
    now = _now()
    with SessionLocal() as session:
        actions = list(
            session.scalars(
                select(Action).where(
                    Action.task_id == task_id,
                    Action.action_type == "send_message",
                    Action.status == "pending",
                )
            )
        )
        for action in actions:
            action.scheduled_at = now
        task = session.get(Task, task_id)
        if task is not None:
            task.next_run_at = now
        session.commit()
    return len(actions)


def task_detail_message_groups(client: TestClient, headers: dict[str, str], task_id: str) -> list[dict]:
    response = client.get(f"/api/tasks/{task_id}/message-groups?page=1&page_size=100", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def task_detail_ai_cycles(client: TestClient, headers: dict[str, str], task_id: str) -> list[dict]:
    response = client.get(f"/api/tasks/{task_id}/ai-cycles?page=1&page_size=100", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture(autouse=True)
def cleanup_continuous_task_center_tasks():
    reset_listener_runtime_cache()
    _cleanup_open_task_center_runtime()
    yield
    reset_listener_runtime_cache()
    _cleanup_open_task_center_runtime()


def _cleanup_open_task_center_runtime():
    with SessionLocal() as session:
        if "tasks" not in inspect(session.bind).get_table_names():
            return
        for task in session.query(Task).filter(Task.deleted_at.is_(None)):
            task.status = "stopped"
            task.next_run_at = None
        for action in session.query(Action).filter(~Action.status.in_(["success", "failed", "skipped", "unknown_after_send"])):
            action.status = "skipped"
            action.executed_at = datetime.now(UTC).replace(tzinfo=None)
            action.result = {"success": False, "error_code": "test_cleanup", "error_message": "test cleanup"}
        session.query(ReviewQueue).filter(ReviewQueue.status == "pending").delete(synchronize_session=False)
        setting = session.query(SchedulingSetting).filter_by(tenant_id=1).first()
        if setting:
            setting.default_account_hour_limit = 0
            setting.default_account_day_limit = 0
            setting.default_account_cooldown_seconds = 0
        session.commit()


def auth_headers(client: TestClient, email: str = "admin@demo.local", password: str = "admin123") -> dict[str, str]:
    challenge = client.get("/api/auth/captcha/challenge")
    assert challenge.status_code == 200, challenge.text
    challenge_body = challenge.json()
    assert "target_value" not in challenge_body
    assert "image_data_url" in challenge_body
    captcha_value = get_challenge_target(challenge_body["challenge_id"])
    assert captcha_value is not None
    captcha = client.post(
        "/api/auth/captcha/verify",
        json={"challenge_id": challenge_body["challenge_id"], "captcha_value": captcha_value},
    )
    assert captcha.status_code == 200, captcha.text
    captcha_token = captcha.json()["captcha_token"]
    response = client.post("/api/auth/login", json={"email": email, "password": password, "captcha_token": captcha_token})
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def enable_mock_ai_provider(client: TestClient, headers: dict[str, str], name: str = "pytest AI") -> dict:
    provider = client.post(
        "/api/ai-providers",
        headers=headers,
        json={
            "provider_name": name,
            "provider_type": "openai_compatible",
            "base_url": f"mock://{uuid4().hex[:8]}",
            "model_name": "pytest-chat",
            "api_key": "pytest",
            "api_key_header": "Authorization",
        },
    ).json()
    client.patch(
        "/api/tenant-ai-settings?tenant_id=1",
        headers=headers,
        json={"default_provider_id": provider["id"], "ai_enabled": True, "fallback_to_mock": False, "temperature": 0.8, "max_tokens": 512},
    )
    return provider


def login_response(client: TestClient, identifier: str, password: str):
    challenge = client.get("/api/auth/captcha/challenge")
    challenge_body = challenge.json()
    captcha_value = get_challenge_target(challenge_body["challenge_id"])
    captcha = client.post(
        "/api/auth/captcha/verify",
        json={"challenge_id": challenge_body["challenge_id"], "captcha_value": captcha_value},
    )
    return client.post(
        "/api/auth/login",
        json={"email": identifier, "password": password, "captcha_token": captcha.json()["captcha_token"]},
    )


def test_worker_main_once_drains_single_iteration(monkeypatch, capsys):
    from app import worker

    calls: list[int] = []
    monkeypatch.setattr(worker, "drain_once", lambda limit=100, **_kwargs: calls.append(limit) or 7)

    assert worker.main(["--once", "--limit", "3"]) == 0
    assert calls == [3]
    out = capsys.readouterr().out
    assert "role=all" in out
    assert "processed=7" in out


def test_worker_drain_once_api_accepts_role(monkeypatch):
    from app.api.routers import system as system_router

    calls: list[tuple[int, str | None]] = []
    monkeypatch.setattr(system_router, "drain_once", lambda limit=100, *, role=None: calls.append((limit, role)) or 4)

    with TestClient(app) as client:
        headers = auth_headers(client)
        response = client.post("/api/worker/drain-once?role=metrics", headers=headers, json={"reason": "测试手动 drain"})

    assert response.status_code == 200, response.text
    assert response.json() == {"processed": 4, "role": "metrics"}
    assert calls == [(100, "metrics")]


def test_worker_drain_once_api_rejects_unknown_role(monkeypatch):
    from app.api.routers import system as system_router

    def fake_drain_once(*_args, **_kwargs):
        raise ValueError("unsupported worker role: nope")

    monkeypatch.setattr(system_router, "drain_once", fake_drain_once)

    with TestClient(app) as client:
        headers = auth_headers(client)
        response = client.post("/api/worker/drain-once?role=nope", headers=headers, json={"reason": "测试手动 drain"})

    assert response.status_code == 400
    assert "unsupported worker role" in response.json()["detail"]


def test_worker_loop_can_stop_after_iterations(monkeypatch):
    from app import worker

    calls: list[int] = []
    sleeps: list[float] = []
    monkeypatch.setattr(worker, "drain_once", lambda limit=100, **_kwargs: calls.append(limit) or 0)
    monkeypatch.setattr(worker.time, "sleep", lambda seconds: sleeps.append(seconds))

    worker.run_worker(limit=5, interval_seconds=0.2, max_iterations=2)

    assert calls == [5, 5]
    assert sleeps == [0.2]


def test_worker_loop_can_stop_with_event(monkeypatch):
    from app import worker
    import threading

    calls: list[int] = []
    stop_event = threading.Event()

    def fake_drain_once(limit=100, **_kwargs):
        calls.append(limit)
        stop_event.set()
        return 0

    monkeypatch.setattr(worker, "drain_once", fake_drain_once)
    worker.run_worker(limit=4, interval_seconds=5, stop_event=stop_event)

    assert calls == [4]


def test_listener_center_events_errors_and_reset_watermark_api():
    with TestClient(app) as client:
        headers = auth_headers(client)
        with SessionLocal() as session:
            session.add(TgGroup(id=7007, tenant_id=1, tg_peer_id="-1007007", title="pytest listener group", auth_status="已授权运营", listener_enabled=True, listener_last_polled_at=datetime(2026, 5, 11, 10, 5, 0), listener_last_error="poll failed"))
            session.add(TgAccount(id=7012, tenant_id=1, display_name="pytest listener", username="pytest_listener", phone_masked="7012", status=AccountStatus.ACTIVE.value, health_score=80))
            session.flush()
            session.add(TgGroupAccount(id=7071, tenant_id=1, group_id=7007, account_id=7012, can_send=True, is_listener=True))
            session.add(
                GroupContextMessage(
                    tenant_id=1,
                    group_id=7007,
                    listener_account_id=7012,
                    sender_peer_id="sender-api",
                    sender_name="API 来源",
                    sender_username="api_sender",
                    sender_role="admin",
                    is_bot=True,
                    content="接口事件",
                    message_type="text",
                    remote_message_id="api-m1",
                    sent_at=datetime(2026, 5, 11, 10, 0, 0),
                )
            )
            session.add(
                ListenerSourceState(
                    tenant_id=1,
                    source_type="group",
                    source_peer_id="-1007007",
                    account_id=7012,
                    shard_key="group:-1007007",
                    last_remote_message_id="api-m1",
                    last_event_at=datetime(2026, 5, 11, 10, 0, 0),
                    backfill_until=datetime(2026, 5, 11, 9, 0, 0),
                    last_error="state failed",
                )
            )
            session.add(Task(id="pytest-listener-task", tenant_id=1, name="pytest listener task", type="group_relay", status="running", type_config={"source_groups": [{"group_id": 7007, "is_active": True}], "monitor_account_ids": [7012]}))
            session.commit()

        events = client.get("/api/listeners/group/7007/events", headers=headers)
        errors = client.get("/api/listeners/group/7007/errors", headers=headers)
        rejected = client.post("/api/listeners/group/7007/reset-watermark", headers=headers, json={"reason": "pytest", "confirm_text": ""})
        reset = client.post("/api/listeners/group/7007/reset-watermark", headers=headers, json={"reason": "pytest reset watermark", "confirm_text": "确认重置"})

        assert events.status_code == 200, events.text
        assert events.json()[0]["sender_peer_id"] == "sender-api"
        assert events.json()[0]["sender_username"] == "api_sender"
        assert events.json()[0]["is_bot"] is True
        assert errors.status_code == 200, errors.text
        assert {item["error_message"] for item in errors.json()} == {"poll failed", "state failed"}
        assert rejected.status_code == 400
        assert "请输入确认重置" in rejected.text
        assert reset.status_code == 200, reset.text
        reset_row = next(item for item in reset.json()["items"] if item["key"] == "group:7007")
        assert reset_row["last_error"] == ""
        with SessionLocal() as session:
            group = session.get(TgGroup, 7007)
            state = session.query(ListenerSourceState).filter_by(source_peer_id="-1007007").first()
            audit_log = session.query(AuditLog).filter_by(target_id="7007", action="重置监听水位").order_by(AuditLog.id.desc()).first()
            assert group.listener_last_polled_at is None
            assert group.listener_last_error == ""
            assert state.last_remote_message_id == ""
            assert state.last_error == ""
            assert audit_log is not None
            assert "pytest reset watermark" in audit_log.detail


def ensure_developer_app(client: TestClient, headers: dict[str, str]) -> dict:
    with SessionLocal() as session:
        tenant = session.get(Tenant, 1)
        if tenant:
            tenant.account_quota = 0
            tenant.task_quota = max(tenant.task_quota, 10000)
            session.commit()
    apps = client.get("/api/developer-apps", headers=headers).json()
    healthy = [app for app in apps if app["is_active"] and app["health_status"] == "健康"]
    if healthy:
        with SessionLocal() as session:
            db_app = session.get(TelegramDeveloperApp, healthy[0]["id"])
            if db_app and db_app.max_accounts < 5000:
                db_app.max_accounts = 5000
                session.commit()
        return healthy[0]
    suffix = int(uuid4().int % 100000)
    response = client.post(
        "/api/developer-apps",
        headers=headers,
        json={
            "app_name": f"测试开发者应用 {suffix}",
            "api_id": 700000 + suffix,
            "api_hash": f"test_api_hash_secret_{suffix}",
            "max_accounts": 5000,
            "notes": "pytest",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def ensure_test_workspace(client: TestClient, headers: dict[str, str]) -> tuple[dict, dict]:
    ensure_developer_app(client, headers)
    suffix = uuid4().hex[:8]
    for _ in range(20):
        response = client.post(
            "/api/tg-accounts",
            headers=headers,
            json={
                "tenant_id": 1,
                "display_name": f"本地测试账号 {suffix}",
                "username": f"local_test_{suffix}",
                "phone_number": _next_test_phone(),
            },
        )
        if response.status_code == 200:
            break
        assert "手机号已存在" in response.text, response.text
    assert response.status_code == 200, response.text
    account = response.json()

    if account["status"] != AccountStatus.ACTIVE.value:
        client.post(f"/api/tg-accounts/{account['id']}/login/start", headers=headers, json={"method": "qr"})
        account = client.post(f"/api/tg-accounts/{account['id']}/login/qr/check", headers=headers).json()

    groups = client.post(f"/api/tg-accounts/{account['id']}/sync-groups", headers=headers).json()
    with SessionLocal() as session:
        for record in session.query(TgAccountSyncRecord).filter_by(account_id=account["id"], status="排队中"):
            record.status = "已同步"
        session.commit()
    group = groups[0]
    if group["auth_status"] != "已授权运营":
        group = client.post(
            f"/api/groups/{group['id']}/authorize",
            headers=headers,
            json={"auth_status": "已授权运营"},
        ).json()
    with SessionLocal() as session:
        db_account = session.get(TgAccount, account["id"])
        if db_account:
            db_account.status = AccountStatus.ACTIVE.value
            db_account.session_ciphertext = db_account.session_ciphertext or f"pytest-session-{account['id']}"
            online_state = session.query(TgAccountOnlineState).filter_by(tenant_id=1, account_id=account["id"]).first()
            if online_state is None:
                online_state = TgAccountOnlineState(tenant_id=1, account_id=account["id"])
                session.add(online_state)
            online_state.desired_online = True
            online_state.online_status = "online"
            online_state.stale_after_at = _now() + timedelta(minutes=30)
        db_group = session.get(TgGroup, group["id"])
        if db_group:
            db_group.auth_status = "已授权运营"
            db_group.can_send = True
            db_group.daily_limit = 10000
            db_group.group_cooldown_seconds = 0
            db_group.banned_words = ""
            db_group.link_whitelist = ""
        link = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=account["id"]).first()
        if link:
            link.can_send = True
            link.is_listener = True
        session.commit()
    return account, group


def make_isolated_ai_group(account_id: int, label: str) -> dict:
    suffix = uuid4().hex[:10]
    with SessionLocal() as session:
        group = TgGroup(
            tenant_id=1,
            tg_peer_id=f"pytest-ai-{suffix}",
            title=f"{label}-{suffix}",
            auth_status="已授权运营",
            can_send=True,
            require_review=False,
            daily_limit=10000,
            account_cooldown_seconds=0,
            group_cooldown_seconds=0,
            listener_enabled=True,
            listener_interval_seconds=0,
        )
        session.add(group)
        session.flush()
        session.add(
            TgGroupAccount(
                tenant_id=1,
                group_id=group.id,
                account_id=account_id,
                can_send=True,
                is_listener=True,
                permission_label="可发言",
            )
        )
        session.commit()
        return {"id": group.id, "title": group.title, "tg_peer_id": group.tg_peer_id}


def mark_test_channel_comment_ready(channel_target_id: int, account_ids: list[int]) -> None:
    with SessionLocal() as session:
        channel = session.get(OperationTarget, channel_target_id)
        assert channel is not None
        group = session.query(TgGroup).filter_by(tenant_id=channel.tenant_id, tg_peer_id=channel.tg_peer_id).first()
        if group is None:
            group = TgGroup(
                tenant_id=channel.tenant_id,
                tg_peer_id=channel.tg_peer_id,
                title=channel.title,
                group_type="channel",
                auth_status="已授权运营",
                can_send=True,
            )
            session.add(group)
            session.flush()
        group.auth_status = "已授权运营"
        group.can_send = True
        for account_id in account_ids:
            account = session.get(TgAccount, account_id)
            assert account is not None
            account.username = account.username or f"pytest_comment_{account_id}"
            account.tg_first_name = account.tg_first_name or f"评论号{account_id}"
            account.avatar_object_key = account.avatar_object_key or f"avatars/test-comment-{account_id}.jpg"
            account.profile_sync_status = "已同步"
            link = session.query(TgGroupAccount).filter_by(group_id=group.id, account_id=account_id).first()
            if link is None:
                link = TgGroupAccount(tenant_id=channel.tenant_id, group_id=group.id, account_id=account_id)
                session.add(link)
            link.can_send = True
            link.permission_label = "普通成员"
        session.commit()


def test_clean_seed_requires_config_before_account_create():
    with TestClient(app) as client:
        headers = auth_headers(client)
        runtime = client.get("/api/config/runtime", headers=headers).json()
        assert runtime["can_create_tg_account"] is False
        assert runtime["developer_app_count"] == 0
        assert runtime["ai_provider_count"] == 0

        blocked = client.post(
            "/api/tg-accounts",
            headers=headers,
            json={"tenant_id": 1, "display_name": "未配置账号", "phone_number": "+8613800000000"},
        )
        assert blocked.status_code == 400
        assert "开发者应用" in blocked.text


def test_verification_tasks_backfill_group_target_label():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            stale_task = VerificationTask(
                tenant_id=1,
                account_id=account["id"],
                group_id=group["id"],
                message_task_id=None,
                verification_type="群发言不可用",
                detected_reason="pytest target label",
                suggested_action="人工处理",
                target_peer_id="",
                target_display="",
                status="待处理",
            )
            session.add(stale_task)
            session.commit()
            stale_task_id = stale_task.id

        tasks = client.get("/api/verification-tasks", headers=headers).json()
        task = next(item for item in tasks if item["id"] == stale_task_id)
        assert task["target_display"] == group["title"]
        assert task["target_peer_id"] == group["tg_peer_id"]
        assert task["issue_category"] == "group_restriction"

        with SessionLocal() as session:
            auto_task = VerificationTask(
                tenant_id=1,
                account_id=account["id"],
                group_id=group["id"],
                message_task_id=None,
                verification_type="机器人按钮验证",
                detected_reason="pytest auto verification",
                suggested_action="点击按钮",
                status="待处理",
            )
            session.add(auto_task)
            session.commit()
            auto_task_id = auto_task.id

        tasks = client.get("/api/verification-tasks", headers=headers).json()
        auto = next(item for item in tasks if item["id"] == auto_task_id)
        assert auto["issue_category"] == "verification"
        assert auto["can_auto_resolve"] is True
        assert auto["resolution_entry_label"] == "执行自动处理"


def test_manual_group_restriction_confirm_does_not_restore_sendability():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.DISABLED.value
            db_group = session.get(TgGroup, group["id"])
            db_group.can_send = False
            link = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=account["id"]).first()
            link.can_send = False
            manual_task = VerificationTask(
                tenant_id=1,
                account_id=account["id"],
                group_id=group["id"],
                message_task_id=None,
                verification_type="群发言不可用",
                detected_reason="pytest manual verification",
                suggested_action="人工处理",
                status="待处理",
            )
            session.add(manual_task)
            session.commit()
            task_id = manual_task.id

        confirmed = client.post(f"/api/verification-tasks/{task_id}/confirm-action", headers=headers, json={"actor": "pytest"}).json()
        assert confirmed["status"] == "需人工处理"
        assert confirmed["issue_category"] == "group_restriction"
        assert confirmed["resolution_entry_label"] == "解除群限制"
        assert "解除限制" in confirmed["failure_detail"]

        with SessionLocal() as session:
            db_group = session.get(TgGroup, group["id"])
            link = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=account["id"]).first()
            assert db_group.can_send is False
            assert link.can_send is False


def test_resolve_group_restriction_rechecks_target_before_restoring_sendability(monkeypatch):
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            db_group = session.get(TgGroup, group["id"])
            db_group.can_send = False
            db_group.auth_status = "只读"
            link = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=account["id"]).first()
            link.can_send = False
            link.permission_label = "群管理限制"
            target = session.query(OperationTarget).filter_by(tenant_id=1, tg_peer_id=group["tg_peer_id"]).first()
            if not target:
                target = OperationTarget(
                    tenant_id=1,
                    target_type="group",
                    tg_peer_id=group["tg_peer_id"],
                    title="运营目标名",
                    member_count=group["member_count"],
                )
                session.add(target)
                session.flush()
            target.title = "运营目标名"
            target.can_send = False
            target.auth_status = "只读"
            manual_task = VerificationTask(
                tenant_id=1,
                account_id=account["id"],
                group_id=group["id"],
                message_task_id=None,
                verification_type="群发言不可用",
                detected_reason="pytest resolve group restriction",
                suggested_action="人工处理",
                status="需人工处理",
            )
            session.add(manual_task)
            session.commit()
            task_id = manual_task.id
            target_id = target.id

        def fake_probe_target_capabilities(account_id, target_peer_id, target_type, *_args, **_kwargs):
            assert account_id == account["id"]
            assert target_peer_id == group["tg_peer_id"]
            assert target_type == "group"
            return OperationResult(True, detail="group:target:可访问")

        monkeypatch.setattr("app.services.verification.gateway.probe_target_capabilities", fake_probe_target_capabilities)

        resolved = client.post(f"/api/verification-tasks/{task_id}/resolve-group-restriction", headers=headers, json={"actor": "pytest"}).json()
        assert resolved["status"] == "已处理"
        assert "重查通过" in resolved["failure_detail"]
        assert resolved["target_display"] == "运营目标名"

        with SessionLocal() as session:
            db_group = session.get(TgGroup, group["id"])
            link = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=account["id"]).first()
            target = session.get(OperationTarget, target_id)
            assert db_group.can_send is True
            assert link.can_send is True
            assert link.permission_label == "可发言"
            assert target.title == "运营目标名"
            assert target.can_send is True
            assert target.auth_status == "已授权运营"


def test_group_verification_response_sends_answer_and_rechecks(monkeypatch):
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            db_group = session.get(TgGroup, group["id"])
            db_group.can_send = False
            db_group.auth_status = "只读"
            link = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=account["id"]).first()
            link.can_send = False
            link.permission_label = "等待验证码"
            manual_task = VerificationTask(
                tenant_id=1,
                account_id=account["id"],
                group_id=group["id"],
                message_task_id=None,
                verification_type="群发言权限",
                detected_reason="验证码：请输入 1234",
                suggested_action="人工处理",
                status="需人工处理",
            )
            session.add(manual_task)
            session.commit()
            task_id = manual_task.id

        def fake_context(account_id, target_peer_id, *_args, **_kwargs):
            assert account_id == account["id"]
            assert target_peer_id == group["tg_peer_id"]
            return [{"message_id": 7, "sender": "验证机器人", "text": "请输入验证码 1234", "sent_at": None}]

        sent = {}

        def fake_submit(account_id, target_peer_id, response_text, *_args, **_kwargs):
            sent.update({"account_id": account_id, "target_peer_id": target_peer_id, "response_text": response_text})
            return OperationResult(True, "已处理", detail="验证回复已发送")

        def fake_probe(account_id, target_peer_id, target_type, *_args, **_kwargs):
            assert target_type == "group"
            return OperationResult(True, detail="group:target:可访问")

        monkeypatch.setattr("app.services.verification.gateway.fetch_verification_context", fake_context)
        monkeypatch.setattr("app.services.verification.gateway.submit_verification_response", fake_submit)
        monkeypatch.setattr("app.services.verification.gateway.probe_target_capabilities", fake_probe)

        context = client.get(f"/api/verification-tasks/{task_id}/challenge-context", headers=headers).json()
        assert context["context_status"] == "ok"
        assert context["message_count"] == 1
        assert context["messages"][0]["text"] == "请输入验证码 1234"

        resolved = client.post(
            f"/api/verification-tasks/{task_id}/submit-response",
            headers=headers,
            json={"actor": "pytest", "response_text": "1234"},
        ).json()
        assert sent == {"account_id": account["id"], "target_peer_id": group["tg_peer_id"], "response_text": "1234"}
        assert resolved["status"] == "已处理"
        assert "重查通过" in resolved["failure_detail"]


def test_group_verification_context_gateway_error_is_explicit(monkeypatch):
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            manual_task = VerificationTask(
                tenant_id=1,
                account_id=account["id"],
                group_id=group["id"],
                message_task_id=None,
                verification_type="群发言权限",
                detected_reason="验证码：请输入 1234",
                suggested_action="人工处理",
                status="需人工处理",
            )
            session.add(manual_task)
            session.commit()
            task_id = manual_task.id

        def unavailable_context(*_args, **_kwargs):
            raise RuntimeError("读取验证聊天失败：The channel specified is private")

        monkeypatch.setattr("app.services.verification.gateway.fetch_verification_context", unavailable_context)

        response = client.get(f"/api/verification-tasks/{task_id}/challenge-context", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["context_status"] == "read_failed"
        assert body["message_count"] == 0
        assert body["read_failure_detail"] == "读取验证聊天失败：The channel specified is private"


def test_group_verification_context_empty_is_explicit(monkeypatch):
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            manual_task = VerificationTask(
                tenant_id=1,
                account_id=account["id"],
                group_id=group["id"],
                message_task_id=None,
                verification_type="群发言权限",
                detected_reason="需要群管理 bot 验证",
                suggested_action="人工处理",
                target_peer_id=group["tg_peer_id"],
                target_display=group["title"],
                status="需人工处理",
            )
            session.add(manual_task)
            session.commit()
            task_id = manual_task.id

        monkeypatch.setattr("app.services.verification.gateway.fetch_verification_context", lambda *_args, **_kwargs: [])

        response = client.get(f"/api/verification-tasks/{task_id}/challenge-context", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["context_status"] == "empty"
        assert body["message_count"] == 0
        assert "没有读取到最近验证聊天信息" in body["read_failure_detail"]


def test_refresh_group_verification_rejoins_reads_with_helper_and_submits_mimo(monkeypatch):
    with TestClient(app) as client:
        headers = auth_headers(client)
        join_account, group = ensure_test_workspace(client, headers)
        reader_account, _ = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            join = session.get(TgAccount, join_account["id"])
            reader = session.get(TgAccount, reader_account["id"])
            db_group = session.get(TgGroup, group["id"])
            join.status = AccountStatus.ACTIVE.value
            reader.status = AccountStatus.ACTIVE.value
            join.session_ciphertext = join.session_ciphertext or "join-session"
            reader.session_ciphertext = reader.session_ciphertext or "reader-session"
            db_group.can_send = False
            join_link = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=join.id).first()
            join_link.can_send = False
            reader_link = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=reader.id).first()
            if not reader_link:
                reader_link = TgGroupAccount(tenant_id=1, group_id=group["id"], account_id=reader.id)
                session.add(reader_link)
            reader_link.can_send = True
            reader_link.permission_label = "可发言"
            manual_task = VerificationTask(
                tenant_id=1,
                account_id=join.id,
                group_id=group["id"],
                message_task_id=None,
                verification_type="群发言权限",
                detected_reason="加入时提示需要群管理 bot 的验证码",
                suggested_action="识别图形验证码",
                target_peer_id=group["tg_peer_id"],
                target_display=group["title"],
                status="需人工处理",
            )
            session.add(manual_task)
            session.commit()
            task_id = manual_task.id

        calls: dict[str, list] = {"join": [], "read": [], "media": [], "submit": [], "probe": []}

        def fake_join(account_id, target_peer_id, *_args, **_kwargs):
            calls["join"].append((account_id, target_peer_id))
            return OperationResult(True, "已处理", detail="已重新加入并触发验证码")

        def fake_probe(account_id, target_peer_id, target_type, *_args, **_kwargs):
            calls["probe"].append((account_id, target_peer_id, target_type))
            if len(calls["probe"]) == 1:
                return OperationResult(False, "需人工处理", FailureType.GROUP_PERMISSION_DENIED.value, "加入时提示需要群管理 bot 的验证码")
            return OperationResult(True, detail="group:target:可访问")

        def fake_context(account_id, *_args, **_kwargs):
            calls["read"].append(account_id)
            if account_id == join_account["id"]:
                raise RuntimeError("读取验证聊天失败：GetHistoryRequest")
            return [{
                "message_id": 9,
                "sender": "群管理 bot",
                "text": "请识别图片验证码",
                "sent_at": None,
                "has_media": True,
                "media_message_id": 9,
                "media_mime_type": "image/png",
                "media_fingerprint": "pytest-image",
            }]

        def fake_media(account_id, _peer, message_id, *_args, **_kwargs):
            calls["media"].append((account_id, message_id))
            from app.integrations.telegram.contracts import CachedMediaResult
            return CachedMediaResult(True, data=b"captcha-image", detail="image/png")

        def fake_submit(account_id, _peer, response_text, *_args, **_kwargs):
            calls["submit"].append((account_id, response_text))
            return OperationResult(True, "已处理", detail="验证码已提交")

        class FakeAnswer:
            answer = "8274"
            confidence = 0.93
            usage = AiUsage()

        monkeypatch.setattr("app.services.verification.gateway.ensure_channel_membership", fake_join)
        monkeypatch.setattr("app.services.verification.gateway.probe_target_capabilities", fake_probe)
        monkeypatch.setattr("app.services.membership_challenges.gateway.fetch_verification_context", fake_context)
        monkeypatch.setattr("app.services.membership_challenges.gateway.fetch_verification_media", fake_media)
        monkeypatch.setattr("app.services.membership_challenges.gateway.submit_verification_response", fake_submit)
        monkeypatch.setattr("app.services.membership_challenges.ai_gateway.solve_image_verification", lambda *_args, **_kwargs: FakeAnswer())
        monkeypatch.setattr("app.services.membership_challenges._mimo_vision_provider", lambda session: type("Provider", (), {"model_name": "mimo-v2.5", "provider_name": "MiMo", "provider_type": "openai_compatible", "base_url": "mock://mimo", "api_key_ciphertext": "", "api_key_header": "Authorization"})())
        monkeypatch.setattr("app.services.verification.credentials_for_account", lambda session, account: DeveloperAppCredentials(1, 12345, "hash", 1))
        monkeypatch.setattr("app.services.membership_challenges.ai_provider_credentials", lambda provider: provider)

        response = client.post(f"/api/verification-tasks/{task_id}/refresh-challenge-context", headers=headers, json={"actor": "pytest"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["context_status"] == "ok"
        assert body["submit_account_id"] == join_account["id"]
        assert body["reader_account_id"] != join_account["id"]
        assert calls["join"] == [(join_account["id"], group["tg_peer_id"])]
        assert calls["read"][:2] == [join_account["id"], body["reader_account_id"]]
        assert calls["media"] == [(body["reader_account_id"], 9)]
        assert calls["submit"] == [(join_account["id"], "8274")]


def test_campaign_draft_approval_and_dispatch_flow():
    skip_legacy_task_center_flow()
    with TestClient(app) as client:
        headers = auth_headers(client)
        _, group = ensure_test_workspace(client, headers)

        campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": group["id"],
                "title": "晚间热群",
                "campaign_type": "话题引导任务",
                "topic": "产品体验反馈",
                "jitter_min_seconds": 0,
                "jitter_max_seconds": 0,
                "batch_interval_seconds": 0,
                "respect_send_window": False,
            },
        ).json()
        assert campaign["status"] == "草稿"

        drafts = client.post(f"/api/campaigns/{campaign['id']}/generate-drafts", headers=headers, json={"count": 2}).json()
        assert len(drafts) == 2
        assert drafts[0]["status"] == "待审核"

        task = client.post(f"/api/ai-drafts/{drafts[0]['id']}/approve", headers=headers, json={"actor": "测试操作员"}).json()
        assert task["status"] == "排队中"

        dispatched = client.post(f"/api/message-tasks/{task['id']}/dispatch", headers=headers).json()
        assert dispatched["status"] in {"已发送", "失败"}


def test_login_flow_masks_verification_state():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _ = ensure_test_workspace(client, headers)
        flow = client.post(f"/api/tg-accounts/{account['id']}/login/start", headers=headers, json={"method": "code", "force": True}).json()
        assert flow["status"] == "等待验证码"
        assert flow["code_preview"]

        account = client.post(f"/api/tg-accounts/{account['id']}/login/verify", headers=headers, json={"code": flow["code_preview"]}).json()
        assert account["status"] == "在线"
        sync_records = client.get(f"/api/tg-accounts/{account['id']}/sync-records", headers=headers).json()
        required_syncs = {"profile_pull", "health", "groups", "contacts", "codes"}
        relevant_records = [record for record in sync_records if record["sync_type"] in required_syncs]
        assert required_syncs.issubset({record["sync_type"] for record in relevant_records})
        assert all(record["status"] != "排队中" for record in relevant_records)
        detail = client.get(f"/api/tg-accounts/{account['id']}/detail", headers=headers).json()
        assert detail["account"]["profile_sync_status"] == "已同步"
        assert detail["contacts"]
        assert detail["groups"]
        targets = client.post(f"/api/tg-accounts/{account['id']}/sync-targets", headers=headers).json()
        assert targets
        assert {target["target_type"] for target in targets} <= {"group", "channel"}
        assert detail["stats"]["pending_verification_tasks"] >= 0


def test_login_start_failure_records_flow_audit_and_structured_error(monkeypatch):
    def fail_login(*_args, **_kwargs):
        raise RuntimeError("telegram connect failed")

    monkeypatch.setattr("app.services.accounts.gateway.start_login", fail_login)

    with TestClient(app) as client:
        headers = auth_headers(client)
        ensure_developer_app(client, headers)
        account = client.post(
            "/api/tg-accounts",
            headers=headers,
            json={"tenant_id": 1, "display_name": "失败登录账号", "phone_number": _next_test_phone()},
        ).json()

        response = client.post(
            f"/api/tg-accounts/{account['id']}/login/start",
            headers=headers,
            json={"method": "code", "force": True},
        )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["message"] == "登录初始化失败，请查看登录流水或联系管理员处理"
    assert detail["failure_type"] == "RuntimeError"
    assert detail["failure_detail"] == "telegram connect failed"
    assert detail["trace_id"]
    with SessionLocal() as session:
        flow = session.query(TgLoginFlow).filter_by(account_id=account["id"]).order_by(TgLoginFlow.id.desc()).first()
        db_account = session.get(TgAccount, account["id"])
        assert flow.status == AccountStatus.ERROR.value
        assert flow.failure_type == "RuntimeError"
        assert flow.failure_detail == "telegram connect failed"
        assert flow.trace_id == detail["trace_id"]
        assert db_account.status == AccountStatus.ERROR.value
        audit = session.query(AuditLog).filter(AuditLog.detail.contains(detail["trace_id"])).first()
        assert audit is not None


def test_account_list_exposes_latest_login_failure_summary():
    with TestClient(app) as client:
        headers = auth_headers(client)
        with SessionLocal() as session:
            account = TgAccount(
                tenant_id=1,
                display_name="验证码没收到账号",
                username="login_code_missing",
                phone_masked=f"login-{uuid4().hex[:8]}",
                status=AccountStatus.ERROR.value,
                health_score=20,
            )
            session.add(account)
            session.flush()
            session.add(
                TgLoginFlow(
                    tenant_id=1,
                    account_id=account.id,
                    method="code",
                    status=AccountStatus.ERROR.value,
                    failure_type="code_not_received",
                    failure_detail="登录验证码没收到，登录失败",
                    trace_id="trace-code-missing",
                    created_at=_now(),
                )
            )
            session.commit()
            account_id = account.id

        response = client.get("/api/tg-accounts?page_size=200", headers=headers)

    assert response.status_code == 200, response.text
    listed = next(item for item in response.json() if item["id"] == account_id)
    flow = listed["latest_login_flow"]
    assert flow["method"] == "code"
    assert flow["status"] == AccountStatus.ERROR.value
    assert flow["failure_type"] == "code_not_received"
    assert flow["failure_detail"] == "登录验证码没收到，登录失败"
    assert flow["trace_id"] == "trace-code-missing"
    assert flow["created_at"]


def test_account_list_search_matches_login_problem_conditions():
    with TestClient(app) as client:
        headers = auth_headers(client)
        with SessionLocal() as session:
            target = TgAccount(
                tenant_id=1,
                display_name="服务端搜索登录问题",
                username="server_search_login_issue",
                phone_masked=f"search-{uuid4().hex[:8]}",
                status=AccountStatus.ERROR.value,
                health_score=20,
            )
            normal = TgAccount(
                tenant_id=1,
                display_name="服务端搜索正常账号",
                username="server_search_normal",
                phone_masked=f"normal-{uuid4().hex[:8]}",
                status=AccountStatus.ACTIVE.value,
                health_score=90,
                session_ciphertext="session",
            )
            session.add_all([target, normal])
            session.flush()
            session.add(
                TgLoginFlow(
                    tenant_id=1,
                    account_id=target.id,
                    method="code",
                    status=AccountStatus.ERROR.value,
                    failure_type="code_not_received",
                    failure_detail="登录验证码没收到，登录失败",
                    trace_id="trace-search-login",
                    created_at=_now(),
                )
            )
            session.commit()
            target_id = target.id
            normal_id = normal.id

        by_code = client.get("/api/tg-accounts?search=验证码没收到&page_size=200", headers=headers)
        by_problem = client.get("/api/tg-accounts?search=登录有问题&page_size=200", headers=headers)

    assert by_code.status_code == 200, by_code.text
    assert target_id in {item["id"] for item in by_code.json()}
    assert normal_id not in {item["id"] for item in by_code.json()}
    assert by_problem.status_code == 200, by_problem.text
    assert target_id in {item["id"] for item in by_problem.json()}


def test_repeated_login_verify_after_success_returns_online_account(monkeypatch):
    calls = 0

    def finish_once_then_expired(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("login flow not started or has expired in this process")
        return AccountStatus.ACTIVE.value, f"encrypted-session:{uuid4().hex}"

    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _ = ensure_test_workspace(client, headers)
        monkeypatch.setattr("app.services.accounts.gateway.finish_login", finish_once_then_expired)
        flow = client.post(f"/api/tg-accounts/{account['id']}/login/start", headers=headers, json={"method": "code", "force": True}).json()

        verified = client.post(f"/api/tg-accounts/{account['id']}/login/verify", headers=headers, json={"code": flow["code_preview"]})
        assert verified.status_code == 200, verified.text
        assert verified.json()["status"] == AccountStatus.ACTIVE.value

        repeated = client.post(f"/api/tg-accounts/{account['id']}/login/verify", headers=headers, json={"code": flow["code_preview"]})
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["status"] == AccountStatus.ACTIVE.value
        assert calls == 1


def test_unfinished_account_soft_delete_allows_phone_reuse():
    with TestClient(app) as client:
        headers = auth_headers(client)
        ensure_developer_app(client, headers)
        phone = f"+86139{uuid4().int % 100000000:08d}"
        created = client.post(
            "/api/tg-accounts",
            headers=headers,
            json={"tenant_id": 1, "display_name": "待移除账号", "phone_number": phone},
        )
        assert created.status_code == 200, created.text
        account = created.json()

        removed = client.delete(f"/api/tg-accounts/{account['id']}", headers=headers)
        assert removed.status_code == 200, removed.text
        removed_body = removed.json()
        assert removed_body["status"] == AccountStatus.DISABLED.value
        assert removed_body["deleted_at"]

        visible = client.get("/api/tg-accounts", headers=headers).json()
        assert account["id"] not in {item["id"] for item in visible}
        with_deleted = client.get("/api/tg-accounts", headers=headers, params={"include_deleted": True}).json()
        assert account["id"] in {item["id"] for item in with_deleted}

        recreated = client.post(
            "/api/tg-accounts",
            headers=headers,
            json={"tenant_id": 1, "display_name": "重新新增账号", "phone_number": phone},
        )
        assert recreated.status_code == 200, recreated.text
        assert recreated.json()["id"] != account["id"]


def test_account_soft_delete_cascades_runtime_state():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        login = client.post(f"/api/tg-accounts/{account['id']}/login/start", headers=headers, json={"method": "code", "force": True}).json()
        assert login["status"] == AccountStatus.WAITING_CODE.value

        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            session.add(TgAccountSyncRecord(tenant_id=1, account_id=account["id"], sync_type="health", trigger_source="pytest", status="排队中", scheduled_at=db_account.created_at, created_at=db_account.created_at))
            session.add(TgAccountProfileSyncRecord(tenant_id=1, account_id=account["id"], actor="pytest", status="排队中"))
            link = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=account["id"]).first()
            assert link
            link.can_send = True
            link.is_listener = True
            session.commit()

        removed = client.delete(f"/api/tg-accounts/{account['id']}", headers=headers)
        assert removed.status_code == 200, removed.text

        with SessionLocal() as session:
            link = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=account["id"]).first()
            assert link.can_send is False
            assert link.is_listener is False
            assert session.query(TgAccountSyncRecord).filter_by(account_id=account["id"], status="已取消").count() >= 1
            assert session.query(TgAccountProfileSyncRecord).filter_by(account_id=account["id"], status="已取消").count() >= 1
            flow = session.query(TgLoginFlow).filter_by(id=login["id"]).first()
            assert flow.status == "已取消"
            assert flow.code_preview is None

        blocked_login = client.post(f"/api/tg-accounts/{account['id']}/login/start", headers=headers, json={"method": "code"})
        assert blocked_login.status_code == 404
        blocked_sync = client.post(f"/api/tg-accounts/{account['id']}/sync-now", headers=headers)
        assert blocked_sync.status_code == 400


def test_expired_code_flow_does_not_block_submitted_code():
    with TestClient(app) as client:
        headers = auth_headers(client)
        ensure_developer_app(client, headers)
        account = client.post(
            "/api/tg-accounts",
            headers=headers,
            json={"tenant_id": 1, "display_name": "验证码登录账号", "phone_number": f"+86137{uuid4().int % 100000000:08d}"},
        ).json()
        flow = client.post(f"/api/tg-accounts/{account['id']}/login/start", headers=headers, json={"method": "code"}).json()
        with SessionLocal() as session:
            db_flow = session.get(TgLoginFlow, flow["id"])
            db_flow.code_expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
            session.commit()

        verified = client.post(f"/api/tg-accounts/{account['id']}/login/verify", headers=headers, json={"code": flow["code_preview"]})
        assert verified.status_code == 200, verified.text
        assert verified.json()["status"] == AccountStatus.ACTIVE.value
        sync_records = client.get(f"/api/tg-accounts/{account['id']}/sync-records", headers=headers).json()
        assert sync_records
        assert all(record["status"] != "排队中" for record in sync_records if record["sync_type"] in {"profile_pull", "health", "groups", "contacts", "codes"})


def test_runtime_login_flows_health_and_group_authorize():
    with TestClient(app) as client:
        headers = auth_headers(client)
        runtime = client.get("/api/config/runtime", headers=headers).json()
        assert runtime["tg_gateway_mode"] in {"mock", "telethon"}

        account, group = ensure_test_workspace(client, headers)
        blocked = client.post(f"/api/tg-accounts/{account['id']}/login/start", headers=headers, json={"method": "qr"})
        assert blocked.status_code == 400
        client.post(f"/api/tg-accounts/{account['id']}/login/start", headers=headers, json={"method": "qr", "force": True})
        flows = client.get(f"/api/tg-accounts/{account['id']}/login-flows", headers=headers).json()
        assert flows
        qr_account = client.post(f"/api/tg-accounts/{account['id']}/login/qr/check", headers=headers).json()
        assert qr_account["status"] == "在线"
        sync_now = client.post(f"/api/tg-accounts/{account['id']}/sync-now", headers=headers)
        assert sync_now.status_code == 200, sync_now.text
        sync_body = sync_now.json()
        assert {record["sync_type"] for record in sync_body}.issuperset({"profile_pull", "health", "groups", "contacts", "codes"})
        assert all(record["status"] != "排队中" for record in sync_body)
        contacts = client.get(f"/api/tg-accounts/{account['id']}/contacts", headers=headers).json()
        assert any(contact.get("phone_number") for contact in contacts)

        checked = client.post(f"/api/tg-accounts/{account['id']}/health-check", headers=headers).json()
        assert checked["status"] in {"在线", "受限", "需重新登录"}

        authorized = client.post(f"/api/groups/{group['id']}/authorize", headers=headers, json={"auth_status": "已授权运营"}).json()
        assert authorized["auth_status"] == "已授权运营"


def test_approve_all_retry_and_archive_detail_flow():
    skip_legacy_task_center_flow()
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            session.commit()
        campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": group["id"],
                "title": "批量审核测试",
                "campaign_type": "定时活跃任务",
                "topic": "FAQ 讨论",
                "jitter_min_seconds": 0,
                "jitter_max_seconds": 0,
                "batch_interval_seconds": 0,
                "respect_send_window": False,
            },
        ).json()
        client.post(f"/api/campaigns/{campaign['id']}/generate-drafts", headers=headers, json={"count": 3})
        tasks = client.post(f"/api/campaigns/{campaign['id']}/approve-all", headers=headers, json={"actor": "测试操作员"}).json()
        assert len(tasks) == 3

        drained = client.post("/api/worker/drain-once", headers=headers, json={"reason": "测试手动 drain"}).json()
        assert drained["processed"] >= 1

        dispatched = client.post(f"/api/message-tasks/{tasks[0]['id']}/dispatch", headers=headers).json()
        assert dispatched["status"] in {"已发送", "失败"}
        if dispatched["status"] == "失败":
            retried = client.post(f"/api/message-tasks/{dispatched['id']}/retry", headers=headers, json={"dispatch_now": False}).json()
            assert retried["status"] == "排队中"

        archive = client.post(
            "/api/archives",
            headers=headers,
            json={"tenant_id": 1, "group_id": group["id"], "title": "归档详情测试"},
        ).json()
        detail = client.get(f"/api/archives/{archive['id']}", headers=headers).json()
        assert detail["messages"]
        assert detail["members"]
        assert any(message.get("sender_phone_number") for message in detail["messages"])
        assert any(member.get("phone_number") for member in detail["members"])


def test_auth_single_admin_and_default_operation_space():
    with TestClient(app) as client:
        headers = auth_headers(client)
        me = client.get("/api/auth/me", headers=headers).json()
        assert me["tenant_id"] == 1
        assert me["role"] == "系统管理员"
        assert me["role_template"] == "系统管理员"
        assert me["permissions"] == ["*"]
        assert me["is_super_admin"] is True
        assert me["permission_version"] >= 1
        assert "subscription_status" not in me
        assert login_response(client, "ops@bootstrap.local", "ops123").status_code == 401

        response = client.get("/api/tg-accounts?tenant_id=999", headers=headers)
        assert response.status_code == 200

        assert client.get("/api/tg-accounts").status_code == 401


def test_authenticated_user_can_change_own_password():
    with TestClient(app) as client:
        headers = auth_headers(client)
        suffix = uuid4().hex[:8]
        email = f"password_user_{suffix}@example.local"
        created = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "name": f"改密用户{suffix}",
                "email": email,
                "password": "oldpass123",
                "role": "后台用户",
                "role_template": "运营人员",
                "permissions": ["overview.view"],
                "menu_permissions": ["overview.view"],
            },
        )
        assert created.status_code == 200, created.text
        user_headers = auth_headers(client, email, "oldpass123")

        wrong_password = client.post(
            "/api/auth/change-password",
            headers=user_headers,
            json={"current_password": "badpass", "new_password": "newpass123"},
        )
        assert wrong_password.status_code == 400

        changed = client.post(
            "/api/auth/change-password",
            headers=user_headers,
            json={"current_password": "oldpass123", "new_password": "newpass123"},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["email"] == email

        assert login_response(client, email, "oldpass123").status_code == 401
        assert login_response(client, email, "newpass123").status_code == 200


def test_admin_users_permission_lifecycle_and_legacy_subscription_endpoints_removed():
    with TestClient(app) as client:
        headers = auth_headers(client)
        old_endpoints = [
            ("post", "/api/auth/register", {"email": "x@example.local", "password": "secret"}),
            ("post", "/api/subscription/redeem", {"code": "NOPE"}),
            ("get", "/api/admin/activation-codes", None),
            ("post", "/api/admin/activation-codes", {"plan_type": "monthly", "quantity": 1}),
            ("get", "/api/admin/subscription-plans", None),
            ("post", "/api/admin/subscription-plans", {"plan_type": "monthly", "name": "Legacy"}),
        ]
        for method, path, payload in old_endpoints:
            request = getattr(client, method)
            response = request(path, headers=headers, json=payload) if payload is not None and method != "get" else request(path, headers=headers)
            assert response.status_code == 404, f"{path} should be removed"

        users_response = client.get("/api/admin/users", headers=headers)
        assert users_response.status_code == 200, users_response.text

        suffix = uuid4().hex[:8]
        create_response = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "name": f"账号添加员{suffix}",
                "email": f"account_creator_{suffix}@example.local",
                "phone": f"+86137{int(uuid4().int % 100000000):08d}",
                "password": "creator123",
                "role": "后台用户",
                "role_template": "账号添加专员",
                "permissions": ["overview.view", "accounts.view", "accounts.create", "accounts.login", "accounts.sync"],
                "menu_permissions": ["overview.view", "accounts.view", "accounts.create", "accounts.login", "accounts.sync"],
            },
        )
        assert create_response.status_code == 200, create_response.text
        created_user = create_response.json()
        assert created_user["role"] == "后台用户"
        assert created_user["role_template"] == "账号添加专员"
        assert "accounts.create" in created_user["permissions"]
        assert "tasks.view" not in created_user["permissions"]

        name_only_response = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "name": f"姓名登录用户{suffix}",
                "password": "namepass123",
                "role": "后台用户",
                "role_template": "只读观察员",
                "permissions": ["overview.view"],
                "menu_permissions": ["overview.view"],
            },
        )
        assert name_only_response.status_code == 200, name_only_response.text
        name_only_user = name_only_response.json()
        assert name_only_user["phone"] is None
        assert name_only_user["email"].endswith("@internal.tg-yunying.local")
        name_only_headers = auth_headers(client, name_only_user["name"], "namepass123")
        name_only_me = client.get("/api/auth/me", headers=name_only_headers)
        assert name_only_me.status_code == 200, name_only_me.text
        assert name_only_me.json()["name"] == name_only_user["name"]

        duplicate_target = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "name": f"重复校验目标{suffix}",
                "email": f"duplicate_target_{suffix}@example.local",
                "phone": f"+86139{int(uuid4().int % 100000000):08d}",
                "password": "duplicate123",
                "role": "后台用户",
                "role_template": "只读观察员",
                "permissions": ["overview.view"],
                "menu_permissions": ["overview.view"],
            },
        )
        assert duplicate_target.status_code == 200, duplicate_target.text
        duplicate_update = client.patch(
            f"/api/admin/users/{duplicate_target.json()['id']}",
            headers=headers,
            json={"email": created_user["email"]},
        )
        assert duplicate_update.status_code == 400
        assert duplicate_update.json()["detail"] == "用户邮箱或手机号已存在"

        account, _ = ensure_test_workspace(client, headers)

        creator_headers = auth_headers(client, created_user["email"], "creator123")
        creator_me = client.get("/api/auth/me", headers=creator_headers)
        assert creator_me.status_code == 200, creator_me.text
        creator_body = creator_me.json()
        assert creator_body["role_template"] == "账号添加专员"
        assert "accounts.create" in creator_body["permissions"]

        allowed_accounts = client.get("/api/tg-accounts", headers=creator_headers)
        assert allowed_accounts.status_code == 200, allowed_accounts.text
        visible_account = next(item for item in allowed_accounts.json() if item["id"] == account["id"])
        assert visible_account["phone_number"]
        assert visible_account["phone_masked"]

        denied_tasks = client.get("/api/tasks", headers=creator_headers)
        assert denied_tasks.status_code == 403
        assert denied_tasks.json()["permission"] == "tasks.view"

        denied_export = client.get("/api/audit-logs/export?reason=权限测试", headers=creator_headers)
        assert denied_export.status_code == 403
        assert denied_export.json()["permission"] == "audit.export"

        denied_codes = client.get(f"/api/tg-accounts/{account['id']}/verification-codes?reason=权限测试", headers=creator_headers)
        assert denied_codes.status_code == 403
        assert denied_codes.json()["permission"] == "accounts.codes.read"

        denied_audits = client.get("/api/audit-logs?keyword=权限拒绝", headers=headers)
        assert denied_audits.status_code == 200, denied_audits.text
        assert any(item["target_id"] == "accounts.codes.read" for item in denied_audits.json())
        assert any(item["target_id"] == "audit.export" for item in denied_audits.json())

        grant_codes = client.patch(
            f"/api/admin/users/{created_user['id']}",
            headers=headers,
            json={
                "permissions": [
                    "overview.view",
                    "accounts.view",
                    "accounts.create",
                    "accounts.login",
                    "accounts.sync",
                    "accounts.codes.read",
                ],
                "menu_permissions": [
                    "overview.view",
                    "accounts.view",
                    "accounts.create",
                    "accounts.login",
                    "accounts.sync",
                    "accounts.view_codes",
                ],
            },
        )
        assert grant_codes.status_code == 200, grant_codes.text
        assert "accounts.codes.read" in grant_codes.json()["permissions"]
        assert "accounts.view_codes" not in grant_codes.json()["permissions"]

        creator_headers = auth_headers(client, created_user["email"], "creator123")
        codes_response = client.get(f"/api/tg-accounts/{account['id']}/verification-codes?reason=权限测试", headers=creator_headers)
        assert codes_response.status_code == 200, codes_response.text
        code_audits = client.get("/api/audit-logs?keyword=查看TG验证码", headers=headers)
        assert code_audits.status_code == 200, code_audits.text
        assert any(item["target_id"] == str(account["id"]) for item in code_audits.json())
        export_ok = client.get("/api/audit-logs/export?reason=权限测试", headers=headers)
        assert export_ok.status_code == 200, export_ok.text
        assert "text/csv" in export_ok.headers["content-type"]

        update_response = client.patch(
            f"/api/admin/users/{created_user['id']}",
            headers=headers,
            json={
                "permissions": ["overview.view", "accounts.view"],
                "menu_permissions": ["overview.view", "accounts.view"],
            },
        )
        assert update_response.status_code == 200, update_response.text
        updated_user = update_response.json()
        assert updated_user["permission_version"] == grant_codes.json()["permission_version"] + 1
        assert "accounts.create" not in updated_user["permissions"]

        stale_token_response = client.get("/api/auth/me", headers=creator_headers)
        assert stale_token_response.status_code == 401

        permission_manager = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "name": f"权限管理员{suffix}",
                "email": f"permission_manager_{suffix}@example.local",
                "phone": f"+86136{int(uuid4().int % 100000000):08d}",
                "password": "permission123",
                "role": "后台用户",
                "role_template": "运营管理员",
                "permissions": ["overview.view", "permissions.view", "permissions.manage"],
                "menu_permissions": ["overview.view", "permissions.view", "permissions.manage"],
            },
        )
        assert permission_manager.status_code == 200, permission_manager.text
        permission_headers = auth_headers(client, permission_manager.json()["email"], "permission123")

        system_guard = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "name": f"系统管理员保护{suffix}",
                "email": f"system_guard_{suffix}@example.local",
                "phone": f"+86135{int(uuid4().int % 100000000):08d}",
                "password": "system123",
                "role": "系统管理员",
                "role_template": "系统管理员",
                "permissions": ["*"],
                "menu_permissions": ["*"],
            },
        )
        assert system_guard.status_code == 200, system_guard.text
        assert system_guard.json()["permissions"] == ["*"]

        denied_system_create = client.post(
            "/api/admin/users",
            headers=permission_headers,
            json={
                "name": f"越权系统管理员{suffix}",
                "email": f"forbidden_system_{suffix}@example.local",
                "password": "forbidden123",
                "role": "系统管理员",
                "role_template": "系统管理员",
                "permissions": ["*"],
            },
        )
        assert denied_system_create.status_code == 403

        denied_permission_grant = client.post(
            "/api/admin/users",
            headers=permission_headers,
            json={
                "name": f"越权权限管理员{suffix}",
                "email": f"forbidden_permission_{suffix}@example.local",
                "password": "forbidden123",
                "role": "后台用户",
                "role_template": "运营管理员",
                "permissions": ["overview.view", "permissions.manage"],
            },
        )
        assert denied_permission_grant.status_code == 403

        denied_star_grant = client.post(
            "/api/admin/users",
            headers=permission_headers,
            json={
                "name": f"越权星号权限{suffix}",
                "email": f"forbidden_star_{suffix}@example.local",
                "password": "forbidden123",
                "role": "后台用户",
                "role_template": "运营管理员",
                "permissions": ["*"],
            },
        )
        assert denied_star_grant.status_code == 403

        denied_patch_permission_manage = client.patch(
            f"/api/admin/users/{created_user['id']}",
            headers=permission_headers,
            json={"permissions": ["overview.view", "permissions.manage"]},
        )
        assert denied_patch_permission_manage.status_code == 403

        denied_patch_system = client.patch(
            f"/api/admin/users/{system_guard.json()['id']}",
            headers=permission_headers,
            json={"name": "非系统管理员不能维护系统管理员"},
        )
        assert denied_patch_system.status_code == 403
        denied_reset_system = client.post(
            f"/api/admin/users/{system_guard.json()['id']}/reset-password",
            headers=permission_headers,
            json={"new_password": "blocked123"},
        )
        assert denied_reset_system.status_code == 403

        developer_manager = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "name": f"开发应用管理员{suffix}",
                "email": f"developer_manager_{suffix}@example.local",
                "phone": f"+86138{int(uuid4().int % 100000000):08d}",
                "password": "manager123",
                "role": "后台用户",
                "role_template": "运营管理员",
                "permissions": ["overview.view", "system.view", "developer_apps.manage"],
                "menu_permissions": ["overview.view", "system.view", "developer_apps.manage"],
            },
        )
        assert developer_manager.status_code == 200, developer_manager.text
        developer_headers = auth_headers(client, developer_manager.json()["email"], "manager123")
        apps_read = client.get("/api/developer-apps", headers=developer_headers)
        assert apps_read.status_code == 200, apps_read.text
        created_app = client.post(
            "/api/developer-apps",
            headers=developer_headers,
            json={
                "app_name": f"开发应用权限内新增{suffix}",
                "api_id": 900000 + int(uuid4().int % 10000),
                "api_hash": "secret_with_developer_app_permission",
                "max_accounts": 10,
                "notes": "pytest",
            },
        )
        assert created_app.status_code == 200, created_app.text
        target_app = created_app.json()
        patched_app = client.patch(
            f"/api/developer-apps/{target_app['id']}",
            headers=developer_headers,
            json={"api_hash": "rotated_with_developer_app_permission"},
        )
        assert patched_app.status_code == 200, patched_app.text
        assert patched_app.json()["credentials_version"] == target_app["credentials_version"] + 1

        material_manager = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "name": f"素材管理员{suffix}",
                "email": f"material_manager_{suffix}@example.local",
                "phone": f"+86134{int(uuid4().int % 100000000):08d}",
                "password": "material123",
                "role": "后台用户",
                "role_template": "运营管理员",
                "permissions": ["overview.view", "materials.view", "materials.upload", "materials.manage"],
                "menu_permissions": ["overview.view", "materials.view", "materials.upload", "materials.manage"],
            },
        )
        assert material_manager.status_code == 200, material_manager.text
        assert "materials.view" in material_manager.json()["permissions"]
        assert "materials.upload" in material_manager.json()["permissions"]
        material_headers = auth_headers(client, material_manager.json()["email"], "material123")
        material_read = client.get("/api/materials", headers=material_headers)
        assert material_read.status_code == 200, material_read.text
        material_created = client.post(
            "/api/materials",
            headers=material_headers,
            json={
                "tenant_id": 1,
                "title": f"素材中心权限素材{suffix}",
                "material_type": "表情包",
                "content": "https://example.local/stickers/material-center.webp",
                "tags": "pytest,素材中心",
            },
        )
        assert material_created.status_code == 200, material_created.text
        material_disabled = client.post(
            f"/api/materials/{material_created.json()['id']}/disable",
            headers=material_headers,
            json={"reason": "pytest 禁用引用保护"},
        )
        assert material_disabled.status_code == 200, material_disabled.text
        assert material_disabled.json()["review_status"] == "已禁用"
        material_restored = client.post(f"/api/materials/{material_created.json()['id']}/restore", headers=material_headers)
        assert material_restored.status_code == 200, material_restored.text
        assert material_restored.json()["review_status"] == "已审核"
        archive = BytesIO()
        with ZipFile(archive, "w") as zip_file:
            zip_file.writestr("zip-one.png", b"\x89PNG\r\n\x1a\nzip-image")
            zip_file.writestr("readme.txt", b"not-image")
        zip_import = client.post(
            "/api/materials/upload/zip",
            headers=material_headers,
            data={"title": "权限图包", "material_type": "图片", "tags": "zip"},
            files={"file": ("materials.zip", archive.getvalue(), "application/zip")},
        )
        assert zip_import.status_code == 200, zip_import.text
        assert zip_import.json()["success_count"] == 1
        import_result = client.get(f"/api/material-imports/{zip_import.json()['import_id']}", headers=material_headers)
        assert import_result.status_code == 200, import_result.text
        assert import_result.json()["skipped_count"] == 1
        import_results = client.get("/api/material-imports", headers=material_headers)
        assert import_results.status_code == 200, import_results.text
        assert import_results.json()[0]["import_id"] == zip_import.json()["import_id"]

        cache_config = client.patch(
            "/api/materials/cache/config",
            headers=headers,
            json={"material_cache_input": "https://t.me/c/1234567890/1", "source_media_cache_input": "@source_cache"},
        )
        assert cache_config.status_code == 200, cache_config.text
        assert cache_config.json()["material_cache"]["normalized_peer"] == "-1001234567890"

        system_only = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "name": f"系统只读{suffix}",
                "email": f"system_only_{suffix}@example.local",
                "phone": f"+86133{int(uuid4().int % 100000000):08d}",
                "password": "systemonly123",
                "role": "后台用户",
                "role_template": "运营管理员",
                "permissions": ["overview.view", "system.view"],
                "menu_permissions": ["overview.view", "system.view"],
            },
        )
        assert system_only.status_code == 200, system_only.text
        system_only_headers = auth_headers(client, system_only.json()["email"], "systemonly123")
        material_denied = client.get("/api/materials", headers=system_only_headers)
        assert material_denied.status_code == 403
        assert material_denied.json()["permission"] == "materials.view"
        config_read = client.get("/api/materials/cache/config", headers=system_only_headers)
        assert config_read.status_code == 200, config_read.text
        config_write_denied = client.patch(
            "/api/materials/cache/config",
            headers=system_only_headers,
            json={"material_cache_input": "@denied"},
        )
        assert config_write_denied.status_code == 403
        assert config_write_denied.json()["permission"] == "system.manage"


def test_message_sending_manage_permission_controls_write_endpoints():
    with TestClient(app) as client:
        headers = auth_headers(client)
        suffix = uuid4().hex[:8]
        manager_response = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "name": f"消息发送管理员{suffix}",
                "email": f"message_manager_{suffix}@example.local",
                "password": "message123",
                "role": "后台用户",
                "role_template": "运营管理员",
                "permissions": ["overview.view", "message_sending.view", "message_sending.manage"],
                "menu_permissions": ["overview.view", "message_sending.view", "message_sending.manage"],
            },
        )
        assert manager_response.status_code == 200, manager_response.text
        manager_headers = auth_headers(client, manager_response.json()["email"], "message123")
        manager_me = client.get("/api/auth/me", headers=manager_headers)
        assert manager_me.status_code == 200, manager_me.text
        assert "message_sending.manage" in manager_me.json()["permissions"]
        assert "message_sending.create" not in manager_me.json()["permissions"]

        viewer_response = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "name": f"消息发送只读{suffix}",
                "email": f"message_viewer_{suffix}@example.local",
                "password": "message123",
                "role": "后台用户",
                "role_template": "只读观察员",
                "permissions": ["overview.view", "message_sending.view"],
                "menu_permissions": ["overview.view", "message_sending.view"],
            },
        )
        assert viewer_response.status_code == 200, viewer_response.text
        viewer_headers = auth_headers(client, viewer_response.json()["email"], "message123")

        allowed_create = client.post("/api/message-send-tasks", headers=manager_headers, json={})
        assert allowed_create.status_code != 403
        denied_create = client.post("/api/message-send-tasks", headers=viewer_headers, json={})
        assert denied_create.status_code == 403
        assert denied_create.json()["permission"] == "message_sending.manage"

        for path in ["/api/message-tasks/999999/dispatch", "/api/message-tasks/999999/retry", "/api/message-tasks/999999/cancel"]:
            payload = {"dispatch_now": False} if path.endswith("/retry") else {"actor": "pytest"}
            allowed = client.post(path, headers=manager_headers, json=payload)
            assert allowed.status_code != 403, f"{path} should accept message_sending.manage before resource validation"
            denied = client.post(path, headers=viewer_headers, json=payload)
            assert denied.status_code == 403, path
            assert denied.json()["permission"] == "message_sending.manage"


def test_operation_issue_read_only_can_view_but_not_change_status():
    with TestClient(app) as client:
        headers = auth_headers(client)
        suffix = uuid4().hex[:8]
        viewer_response = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "name": f"异常只读员{suffix}",
                "email": f"issue_viewer_{suffix}@example.local",
                "password": "viewer123",
                "role": "后台用户",
                "role_template": "只读观察员",
                "permissions": ["overview.view"],
                "menu_permissions": ["overview.view"],
            },
        )
        assert viewer_response.status_code == 200, viewer_response.text
        viewer_headers = auth_headers(client, viewer_response.json()["email"], "viewer123")

        issue_list = client.get("/api/operation-issues", headers=viewer_headers)
        assert issue_list.status_code == 200, issue_list.text

        missing_detail = client.get("/api/operation-issues/not-found", headers=viewer_headers)
        assert missing_detail.status_code == 404, missing_detail.text

        for action in ["claim", "acknowledge", "resolve", "ignore"]:
            denied = client.post(
                f"/api/operation-issues/not-found/{action}",
                headers=viewer_headers,
                json={"reason": "只读权限不能处理异常"},
            )
            assert denied.status_code == 403
            assert denied.json()["permission"] == "operation_issues.manage"


def test_sensitive_read_routes_deny_low_permission_direct_calls():
    with TestClient(app) as client:
        headers = auth_headers(client)
        suffix = uuid4().hex[:8]
        viewer_response = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "name": f"敏感只读员{suffix}",
                "email": f"sensitive_viewer_{suffix}@example.local",
                "password": "viewer123",
                "role": "后台用户",
                "role_template": "只读观察员",
                "permissions": ["overview.view"],
                "menu_permissions": ["overview.view"],
            },
        )
        assert viewer_response.status_code == 200, viewer_response.text
        viewer_headers = auth_headers(client, viewer_response.json()["email"], "viewer123")

        denied_routes = [
            ("/api/config/runtime", "system.view"),
            ("/api/account-clone-plans", "accounts.clone"),
            ("/api/verification-tasks", "accounts.sync"),
            ("/api/tg-accounts/1/verification-tasks", "accounts.sync"),
            ("/api/groups/1/verification-tasks", "accounts.sync"),
            ("/api/channel-comments", "targets.view"),
            ("/api/rules/relay-attribution/report", "rules.view"),
        ]
        for path, permission in denied_routes:
            response = client.get(path, headers=viewer_headers)
            assert response.status_code == 403, path
            assert response.json()["permission"] == permission


def test_material_prd_api_surface_detail_versions_references_refresh_and_groups():
    with TestClient(app) as client:
        headers = auth_headers(client)
        suffix = uuid4().hex[:8]
        created = client.post(
            "/api/materials",
            headers=headers,
            json={
                "tenant_id": 1,
                "title": f"素材API对齐{suffix}",
                "material_type": "图片",
                "content": "https://trusted.example.com/material-api-v1.png",
                "tags": f"api,{suffix}",
                "tg_cache_peer_id": "cache-peer",
                "tg_cache_message_id": "1001",
            },
        )
        assert created.status_code == 200, created.text
        material_id = created.json()["id"]

        with SessionLocal() as session:
            session.add(
                Task(
                    id=f"material-api-task-{suffix}",
                    tenant_id=1,
                    name="素材 API 引用任务",
                    type="group_relay",
                    status="active",
                )
            )
            session.flush()
            session.add(
                MessageTask(
                    tenant_id=1,
                    content="素材引用",
                    message_type="图片",
                    material_id=material_id,
                    idempotency_key=f"material-api-{suffix}",
                )
            )
            session.add(
                Action(
                    id=f"material-api-action-{suffix}",
                    tenant_id=1,
                    task_id=f"material-api-task-{suffix}",
                    task_type="group_relay",
                    action_type="send_message",
                    payload={"media_segments": [{"material_id": material_id}]},
                )
            )
            session.commit()

        detail = client.get(f"/api/materials/{material_id}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["id"] == material_id
        assert detail.json()["reference_summary"]["total_count"] == 2

        version_response = client.post(
            f"/api/materials/{material_id}/versions",
            headers=headers,
            json={"content": "https://trusted.example.com/material-api-v2.png", "caption": "第二版"},
        )
        assert version_response.status_code == 200, version_response.text
        assert version_response.json()["asset_version_id"] == 2

        versions = client.get(f"/api/materials/{material_id}/versions", headers=headers)
        assert versions.status_code == 200, versions.text
        assert [item["asset_version_id"] for item in versions.json()["asset_versions"]] == [2, 1]
        assert versions.json()["tg_ref_versions"][0]["tg_ref_version_id"] == version_response.json()["tg_ref_version_id"]

        references = client.get(f"/api/materials/{material_id}/references", headers=headers)
        assert references.status_code == 200, references.text
        assert references.json()["summary"]["message_task_count"] == 1
        assert references.json()["summary"]["action_count"] == 1

        refresh = client.post(
            f"/api/materials/{material_id}/refresh-cache",
            headers=headers,
            json={"reason": "重新缓存素材"},
        )
        assert refresh.status_code == 200, refresh.text
        assert refresh.json()["cache_ready_status"] == "not_cached"
        assert refresh.json()["tg_cache_peer_id"] == ""

        composite = client.post(
            "/api/materials",
            headers=headers,
            json={
                "tenant_id": 1,
                "title": f"组合消息{suffix}",
                "material_type": "组合消息",
                "content": "图文组合",
                "tags": f"api,{suffix}",
            },
        )
        assert composite.status_code == 200, composite.text
        composite_refresh = client.post(
            f"/api/materials/{composite.json()['id']}/refresh-cache",
            headers=headers,
            json={"reason": "组合消息不应进入缓存队列"},
        )
        assert composite_refresh.status_code == 400
        assert "不支持刷新缓存" in composite_refresh.json()["detail"]

        group = client.post(
            "/api/material-groups",
            headers=headers,
            json={"name": f"活动素材{suffix}", "group_type": "图片", "description": "API 分组"},
        )
        assert group.status_code == 200, group.text
        group_id = group.json()["id"]
        groups = client.get("/api/material-groups", headers=headers)
        assert groups.status_code == 200, groups.text
        assert any(item["id"] == group_id for item in groups.json())
        patched_group = client.patch(
            f"/api/material-groups/{group_id}",
            headers=headers,
            json={"name": f"活动素材更新{suffix}", "is_active": False},
        )
        assert patched_group.status_code == 200, patched_group.text
        assert patched_group.json()["name"].startswith("活动素材更新")
        assert patched_group.json()["is_active"] is False


def test_message_send_task_prd_list_detail_and_precheck_endpoints():
    with TestClient(app) as client:
        headers = auth_headers(client)
        suffix = uuid4().hex[:8]
        with SessionLocal() as session:
            account = TgAccount(
                tenant_id=1,
                display_name=f"消息发送接口账号{suffix}",
                username=f"message_api_{suffix}",
                phone_masked=f"+message-api-{suffix}",
                status=AccountStatus.ACTIVE.value,
                health_score=100,
            )
            target = OperationTarget(
                tenant_id=1,
                target_type="group",
                tg_peer_id=f"pytest-message-api-{suffix}",
                title="消息发送接口目标",
                can_send=True,
                auth_status="已授权运营",
            )
            session.add_all([account, target])
            session.flush()
            task = MessageTask(
                tenant_id=1,
                account_id=account.id,
                preferred_account_id=account.id,
                content="PRD 命名接口消息",
                message_type="文本",
                target_type="group",
                target_peer_id=target.tg_peer_id,
                target_display=target.title,
                status=TaskStatus.QUEUED.value,
                idempotency_key=f"pytest-message-api-{suffix}",
                scheduled_at=datetime.now(UTC).replace(tzinfo=None),
            )
            session.add(task)
            session.commit()
            task_id = task.id

        list_response = client.get("/api/message-send-tasks", headers=headers)
        detail_response = client.get(f"/api/message-send-tasks/{task_id}", headers=headers)
        precheck_response = client.post(f"/api/message-send-tasks/{task_id}/precheck", headers=headers)
        missing_dispatch = client.post("/api/message-send-tasks/999999999/dispatch", headers=headers)
        missing_retry = client.post("/api/message-send-tasks/999999999/retry", headers=headers, json={"dispatch_now": False})
        missing_cancel = client.post("/api/message-send-tasks/999999999/cancel", headers=headers, json={"actor": "pytest"})
        missing_detail = client.get("/api/message-send-tasks/999999999", headers=headers)

        assert list_response.status_code == 200, list_response.text
        assert any(item["id"] == task_id for item in list_response.json())
        assert detail_response.status_code == 200, detail_response.text
        detail = detail_response.json()
        assert detail["id"] == task_id
        assert detail["content"] == "PRD 命名接口消息"
        assert detail["operation_issue_rolled_up"] is False
        assert precheck_response.status_code == 200, precheck_response.text
        precheck = precheck_response.json()
        assert precheck["decision"] in {"allow", "warn"}
        assert precheck["available_accounts"][0]["account_id"] == detail["account_id"]
        assert precheck["target_warnings"] == []
        assert missing_dispatch.status_code == 404
        assert missing_retry.status_code == 404
        assert missing_cancel.status_code == 404
        assert missing_detail.status_code == 404


def test_developer_app_admin_crud_hides_api_hash():
    with TestClient(app) as client:
        headers = auth_headers(client)
        api_id = 800000 + int(uuid4().int % 100000)

        created = client.post(
            "/api/developer-apps",
            headers=headers,
            json={
                "app_name": "测试开发者应用",
                "api_id": api_id,
                "api_hash": "test_api_hash_secret",
                "max_accounts": 3,
                "notes": "pytest",
            },
        )
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["api_id"] == api_id
        assert body["health_status"] == "健康"
        assert "api_hash" not in body

        apps = client.get("/api/developer-apps", headers=headers).json()
        assert all("api_hash" not in app for app in apps)

        assert login_response(client, "ops@bootstrap.local", "ops123").status_code == 401

        disabled = client.post(f"/api/developer-apps/{body['id']}/disable", headers=headers).json()
        assert disabled["is_active"] is False
        assert disabled["health_status"] == "禁用"

        enabled = client.post(f"/api/developer-apps/{body['id']}/enable", headers=headers).json()
        assert enabled["is_active"] is True
        assert enabled["health_status"] == "健康"


def test_developer_app_round_robin_assignment_and_version_rotation():
    with TestClient(app) as client:
        headers = auth_headers(client)
        suffix = int(uuid4().int % 100000)
        first = client.post(
            "/api/developer-apps",
            headers=headers,
            json={"app_name": "轮询应用 A", "api_id": 900000 + suffix, "api_hash": "round_robin_secret_a", "max_accounts": 10},
        ).json()
        second = client.post(
            "/api/developer-apps",
            headers=headers,
            json={"app_name": "轮询应用 B", "api_id": 910000 + suffix, "api_hash": "round_robin_secret_b", "max_accounts": 10},
        ).json()

        with SessionLocal() as session:
            apps = list(session.query(TelegramDeveloperApp).all())
            original_states = {app.id: (app.is_active, app.health_status) for app in apps}
            for developer_app in apps:
                if developer_app.id not in {first["id"], second["id"]}:
                    developer_app.is_active = False
                    developer_app.health_status = DeveloperAppHealthStatus.DISABLED.value
            session.commit()

        try:
            phone_tail_a = f"{suffix % 10000:04d}"
            phone_tail_b = f"{(suffix + 1) % 10000:04d}"
            first_account = client.post(
                "/api/tg-accounts",
                headers=headers,
                json={"tenant_id": 1, "display_name": "轮询账号 A", "username": f"rr_a_{suffix}", "phone_number": f"+86138100{phone_tail_a}"},
            ).json()
            second_account = client.post(
                "/api/tg-accounts",
                headers=headers,
                json={"tenant_id": 1, "display_name": "轮询账号 B", "username": f"rr_b_{suffix}", "phone_number": f"+86138100{phone_tail_b}"},
            ).json()
            assert first_account["phone_number"] == f"+86138100{phone_tail_a}"
            assert f"****{phone_tail_a}" in first_account["phone_masked"]

            logged_first = client.post(f"/api/tg-accounts/{first_account['id']}/login/start", headers=headers, json={"method": "qr"}).json()
            logged_second = client.post(f"/api/tg-accounts/{second_account['id']}/login/start", headers=headers, json={"method": "qr"}).json()
            assert logged_first["status"] == "等待扫码"
            assert logged_second["status"] == "等待扫码"

            accounts = client.get("/api/tg-accounts", headers=headers).json()
            account_map = {account["id"]: account for account in accounts}
            assert account_map[first_account["id"]]["developer_app_id"] == first["id"]
            assert account_map[second_account["id"]]["developer_app_id"] == second["id"]

            rotated = client.patch(
                f"/api/developer-apps/{first['id']}",
                headers=headers,
                json={"api_hash": "round_robin_secret_a_rotated"},
            ).json()
            assert rotated["credentials_version"] == first["credentials_version"] + 1

            checked = client.post(f"/api/tg-accounts/{first_account['id']}/health-check", headers=headers).json()
            assert checked["status"] == AccountStatus.NEED_RELOGIN.value
        finally:
            with SessionLocal() as session:
                for app_id, (is_active, health_status) in original_states.items():
                    developer_app = session.get(TelegramDeveloperApp, app_id)
                    if developer_app:
                        developer_app.is_active = is_active
                        developer_app.health_status = health_status
                session.commit()


def test_ai_provider_prompt_material_and_jitter_flow():
    skip_legacy_task_center_flow()
    with TestClient(app) as client:
        headers = auth_headers(client)
        runtime = client.get("/api/config/runtime", headers=headers).json()
        assert "ai_provider_count" in runtime

        providers = client.get("/api/ai-providers", headers=headers).json()
        assert providers == []
        assert all("api_key" not in provider for provider in providers)

        provider = client.post(
            "/api/ai-providers",
            headers=headers,
            json={
                "provider_name": "DeepSeek Mock",
                "provider_type": "openai_compatible",
                "base_url": "mock://openai-compatible",
                "model_name": "deepseek-chat",
                "api_key": "mock_deepseek_key",
                "api_key_header": "Authorization",
            },
        ).json()
        assert provider["health_status"] == "健康"

        template = client.post(
            "/api/prompt-templates",
            headers=headers,
            json={
                "tenant_id": 1,
                "template_type": "群活跃草稿",
                "name": "pytest 群活跃模板",
                "content": "群 {{group_title}} 围绕 {{topic}} 生成 {{count}} 条，素材 {{materials}}，输出 JSON drafts。",
            },
        ).json()
        assert template["version"] == 1

        material = client.post(
            "/api/materials",
            headers=headers,
            json={"tenant_id": 1, "title": "pytest 表情包", "material_type": "表情包", "content": "https://example.local/sticker.webp", "tags": "pytest"},
        ).json()
        assert material["material_type"] == "表情包"

        setting = client.patch(
            "/api/tenant-ai-settings?tenant_id=1",
            headers=headers,
            json={"default_provider_id": provider["id"], "ai_enabled": True, "fallback_to_mock": True, "temperature": 0.7, "max_tokens": 512},
        ).json()
        assert setting["default_provider_id"] == provider["id"]

        _, group = ensure_test_workspace(client, headers)
        campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": group["id"],
                "title": "抖动调度测试",
                "campaign_type": "话题引导任务",
                "topic": "素材配文",
                "send_window": "00:00-23:59",
                "ai_provider_id": provider["id"],
                "prompt_template_id": template["id"],
                "jitter_min_seconds": 10,
                "jitter_max_seconds": 10,
                "batch_interval_seconds": 20,
                "material_ids": str(material["id"]),
            },
        ).json()
        drafts = client.post(f"/api/campaigns/{campaign['id']}/generate-drafts", headers=headers, json={"count": 2, "use_ai": True}).json()
        assert drafts[0]["model_name"] == "deepseek-chat"
        assert drafts[0]["prompt_template_name"].startswith("pytest 群活跃模板")
        assert drafts[0]["material_id"] == material["id"]

        tasks = client.post(f"/api/campaigns/{campaign['id']}/approve-all", headers=headers, json={"actor": "测试操作员"}).json()
        assert [task["planned_delay_seconds"] for task in tasks] == [10, 30]
        assert tasks[0]["message_type"] == "表情包"
        drained = client.post("/api/worker/drain-once", headers=headers, json={"reason": "测试手动 drain"}).json()
        assert drained["processed"] == 0


def test_ai_real_provider_records_campaign_usage_without_user_token_balance(monkeypatch):
    skip_legacy_task_center_flow()
    with TestClient(app) as client:
        headers = auth_headers(client)
        previous_setting = client.get("/api/tenant-ai-settings?tenant_id=1", headers=headers).json()
        provider = client.post(
            "/api/ai-providers",
            headers=headers,
            json={
                "provider_name": f"Real Billing {uuid4().hex[:6]}",
                "provider_type": "openai_compatible",
                "base_url": "https://ai-billing.test",
                "model_name": "deepseek-chat",
                "api_key": "real_token_test_key",
            },
        ).json()
        client.patch(
            "/api/tenant-ai-settings?tenant_id=1",
            headers=headers,
            json={"default_provider_id": provider["id"], "ai_enabled": True, "fallback_to_mock": False, "temperature": 0.7, "max_tokens": 512},
        )
        _, group = ensure_test_workspace(client, headers)
        campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": group["id"],
                "title": f"token-billing-{uuid4().hex[:6]}",
                "campaign_type": "话题引导任务",
                "topic": "Token billing",
                "ai_provider_id": provider["id"],
                "jitter_min_seconds": 0,
                "jitter_max_seconds": 0,
                "batch_interval_seconds": 0,
                "respect_send_window": False,
            },
        ).json()

        def fake_generate(credentials, prompt, *, count, topic, tone, persona_set, temperature, max_tokens, material_ids=None, selected_account_ids=None):
            return AiGenerationResult(
                candidates=mock_candidates(count, topic, tone, persona_set, material_ids, selected_account_ids),
                usage=AiUsage(prompt_tokens=12, completion_tokens=30, total_tokens=42, billable=True),
            )

        monkeypatch.setattr("app.services.campaigns.ai_gateway.generate_drafts", fake_generate)

        generated = client.post(f"/api/campaigns/{campaign['id']}/generate-drafts", headers=headers, json={"count": 1, "use_ai": True})
        assert generated.status_code == 200, generated.text
        with SessionLocal() as session:
            campaign_row = session.get(Campaign, campaign["id"])
            ledger = session.query(AiUsageLedger).filter_by(campaign_id=campaign["id"], request_status="success").order_by(AiUsageLedger.id.desc()).first()
            assert ledger is not None
            assert ledger.user_id == 0
            assert ledger.total_tokens == 42
            assert campaign_row.used_ai_tokens == 42
        client.patch(
            "/api/tenant-ai-settings?tenant_id=1",
            headers=headers,
            json={
                "default_provider_id": previous_setting["default_provider_id"],
                "ai_enabled": previous_setting["ai_enabled"],
                "fallback_to_mock": previous_setting["fallback_to_mock"],
                "temperature": previous_setting["temperature"],
                "max_tokens": previous_setting["max_tokens"],
            },
        )


def test_ai_provider_check_keeps_warning_when_provider_is_healthy(monkeypatch):
    with TestClient(app) as client:
        headers = auth_headers(client)
        provider = client.post(
            "/api/ai-providers",
            headers=headers,
            json={
                "provider_name": f"MiMo Warning {uuid4().hex[:6]}",
                "provider_type": "openai_compatible",
                "base_url": "mock://openai-compatible",
                "model_name": "mimo-v2.5",
                "api_key": "mock_mimo_key",
                "api_key_header": "Authorization",
            },
        ).json()

        monkeypatch.setattr(
            "app.services.ai_config.ai_gateway.check",
            lambda _credentials: (
                True,
                "provider ready; chat capability warning: AI provider returned empty final content; finish_reason=length",
            ),
        )

        checked = client.post(f"/api/ai-providers/{provider['id']}/check", headers=headers)

        assert checked.status_code == 200, checked.text
        body = checked.json()
        assert body["health_status"] == "健康"
        assert body["last_error"].startswith("provider ready; chat capability warning:")


def test_prompt_template_listing_matches_existing_tenant_resolution_rules():
    with TestClient(app) as client:
        headers = auth_headers(client)
        created = client.post(
            "/api/prompt-templates",
            headers=headers,
            json={
                "tenant_id": 1,
                "template_type": "群活跃草稿",
                "name": f"tenant-template-{uuid4().hex[:6]}",
                "content": "tenant scoped template",
            },
        ).json()

        all_visible = client.get("/api/prompt-templates", headers=headers).json()
        assert any(item["id"] == created["id"] for item in all_visible)
        assert any(item["tenant_id"] is None for item in all_visible)

        tenant_visible = client.get("/api/prompt-templates?tenant_id=1", headers=headers).json()
        assert any(item["id"] == created["id"] for item in tenant_visible)

        default_space = client.get("/api/prompt-templates?tenant_id=999", headers=headers)
        assert default_space.status_code == 200


def test_ai_drafts_listing_uses_service_and_preserves_desc_order():
    skip_legacy_task_center_flow()
    with TestClient(app) as client:
        headers = auth_headers(client)
        _, group = ensure_test_workspace(client, headers)
        campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": group["id"],
                "title": f"draft-list-{uuid4().hex[:6]}",
                "campaign_type": "话题引导任务",
                "topic": "draft list order",
                "jitter_min_seconds": 0,
                "jitter_max_seconds": 0,
                "batch_interval_seconds": 0,
                "respect_send_window": False,
            },
        ).json()
        generated = client.post(
            f"/api/campaigns/{campaign['id']}/generate-drafts",
            headers=headers,
            json={"count": 2},
        ).json()

        drafts = client.get("/api/ai-drafts", headers=headers).json()
        returned_ids = [item["id"] for item in drafts]
        assert generated[0]["id"] in returned_ids and generated[1]["id"] in returned_ids
        assert returned_ids == sorted(returned_ids, reverse=True)


def test_ai_provider_write_uses_single_admin():
    with TestClient(app) as client:
        headers = auth_headers(client)
        created = client.post(
            "/api/ai-providers",
            headers=headers,
            json={"provider_name": f"Single Admin {uuid4().hex[:6]}", "base_url": "mock://openai-compatible", "model_name": "x", "api_key": "secret"},
        )
        assert created.status_code == 200, created.text
        assert login_response(client, "ops@bootstrap.local", "ops123").status_code == 401


def test_system_prompt_decision_seed_and_auto_template_selection():
    skip_legacy_task_center_flow()
    with TestClient(app) as client:
        headers = auth_headers(client)
        templates = client.get("/api/prompt-templates", headers=headers).json()
        assert any(template["template_type"] == "系统决策提示词" for template in templates)

        _, group = ensure_test_workspace(client, headers)
        material = client.post(
            "/api/materials",
            headers=headers,
            json={
                "tenant_id": 1,
                "title": "系统决策素材",
                "material_type": "图片",
                "content": "https://example.local/system-decision.png",
                "tags": "system-decision",
            },
        ).json()
        campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": group["id"],
                "title": "系统提示词自动决策",
                "campaign_type": "话题引导任务",
                "topic": "结合素材做一轮自然讨论",
                "jitter_min_seconds": 0,
                "jitter_max_seconds": 0,
                "batch_interval_seconds": 0,
                "respect_send_window": False,
                "material_ids": str(material["id"]),
            },
        ).json()

        drafts = client.post(f"/api/campaigns/{campaign['id']}/generate-drafts", headers=headers, json={"count": 1}).json()
        assert drafts[0]["prompt_template_name"].startswith("默认素材配文")
        assert "默认系统决策提示词" in drafts[0]["prompt_template_name"]


def test_system_prompt_skips_ai_when_tenant_ai_disabled():
    skip_legacy_task_center_flow()
    with TestClient(app) as client:
        headers = auth_headers(client)
        original = client.get("/api/tenant-ai-settings", headers=headers).json()
        client.patch("/api/tenant-ai-settings?tenant_id=1", headers=headers, json={"ai_enabled": False})
        try:
            _, group = ensure_test_workspace(client, headers)
            campaign = client.post(
                "/api/campaigns",
                headers=headers,
                json={
                    "tenant_id": 1,
                    "group_id": group["id"],
                    "title": "AI 关闭走系统跳过",
                    "campaign_type": "定时活跃任务",
                    "topic": "不调用模型也能生成模板草稿",
                    "jitter_min_seconds": 0,
                    "jitter_max_seconds": 0,
                    "batch_interval_seconds": 0,
                    "respect_send_window": False,
                },
            ).json()
            drafts = client.post(f"/api/campaigns/{campaign['id']}/generate-drafts", headers=headers, json={"count": 1}).json()
            assert drafts[0]["generation_source"] == "system_skipped"
            assert drafts[0]["provider_name"] == "系统决策"
        finally:
            client.patch(
                "/api/tenant-ai-settings?tenant_id=1",
                headers=headers,
                json={
                    "default_provider_id": original["default_provider_id"],
                    "ai_enabled": original["ai_enabled"],
                    "fallback_to_mock": original["fallback_to_mock"],
                    "temperature": original["temperature"],
                    "max_tokens": original["max_tokens"],
                },
            )


def test_account_detail_codes_and_direct_message_queue():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _ = ensure_test_workspace(client, headers)

        detail = client.get(f"/api/tg-accounts/{account['id']}/detail", headers=headers).json()
        assert detail["account"]["id"] == account["id"]
        assert "groups" in detail
        assert "message_records" in detail

        codes = client.post(f"/api/tg-accounts/{account['id']}/verification-codes/poll", headers=headers, json={"reason": "测试提取验证码"}).json()
        assert codes
        assert codes[0]["source"] == "telegram_service_message"
        assert codes[0]["code_preview"]
        contacts = client.post(f"/api/tg-accounts/{account['id']}/contacts/sync", headers=headers).json()
        target_contact = next(contact for contact in contacts if contact["username"] == "pytest_target")

        task = client.post(
            f"/api/tg-accounts/{account['id']}/direct-message-tasks",
            headers=headers,
            json={"target_peer_id": f"@{target_contact['username']}", "target_display": target_contact["display_name"], "content": "hello from queue"},
        ).json()
        assert task["target_type"] == "private"
        assert task["target_peer_id"] == "@pytest_target"
        assert task["account_id"] == account["id"]
        assert task["status"] == "排队中"

        records = client.get(f"/api/tg-accounts/{account['id']}/message-records", headers=headers).json()
        assert any(record["id"] == task["id"] for record in records)


def test_account_detail_risk_diagnostics_collects_status_and_failures():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _ = ensure_test_workspace(client, headers)

        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.LIMITED.value
            db_account.health_score = 35
            session.add(
                ManualOperationRecord(
                    tenant_id=1,
                    account_id=account["id"],
                    operation_type="MESSAGE_SEND",
                    content="risk probe",
                    status=TaskStatus.FAILED.value,
                    failure_type=FailureType.FLOOD_WAIT.value,
                    failure_detail="FloodWait 120 秒",
                    actor="pytest",
                )
            )
            session.add(
                ManualOperationRecord(
                    tenant_id=1,
                    account_id=account["id"],
                    operation_type="MESSAGE_SEND",
                    content="limited reason probe",
                    status=TaskStatus.FAILED.value,
                    failure_type=FailureType.ACCOUNT_LIMITED.value,
                    failure_detail="PeerFlood: 该账号被 TG 限制发言",
                    actor="pytest",
                    created_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=1),
                )
            )
            session.commit()

        detail = client.get(f"/api/tg-accounts/{account['id']}/detail", headers=headers).json()
        risks = detail["risk_diagnostics"]
        account_status_risk = next(risk for risk in risks if risk["code"] == "ACCOUNT_STATUS" and risk["title"] == "账号受限")
        assert detail["stats"]["risk_diagnostics"] >= 2
        assert detail["stats"]["high_risk_diagnostics"] >= 1
        assert "PeerFlood" in account_status_risk["detail"]
        assert "等待 TG" in account_status_risk["detail"]
        assert any(risk["title"] == "触发 FloodWait" and "120" in risk["detail"] for risk in risks)


def test_account_profile_upload_save_sync_and_retry():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _ = ensure_test_workspace(client, headers)

        uploaded = client.post(
            f"/api/tg-accounts/{account['id']}/avatar",
            headers=headers,
            files={"file": ("avatar.png", b"\x89PNG\r\n\x1a\npytest-avatar", "image/png")},
        )
        assert uploaded.status_code == 200, uploaded.text
        avatar = uploaded.json()
        assert avatar["object_key"].startswith(f"avatars/1/{account['id']}/")
        assert avatar["preview_url"].startswith("/media/")

        saved = client.patch(
            f"/api/tg-accounts/{account['id']}/profile",
            headers=headers,
            json={
                "display_name": "资料同步账号",
                "tg_first_name": "资料",
                "tg_last_name": "同步",
                "tg_bio": "pytest bio",
                "avatar_object_key": avatar["object_key"],
            },
        )
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["display_name"] == "资料同步账号"
        assert body["profile_sync_status"] == "排队中"

        records = client.get(f"/api/tg-accounts/{account['id']}/profile-sync-records", headers=headers).json()
        assert records
        assert records[0]["status"] == "排队中"

        drained = client.post("/api/worker/drain-once", headers=headers, json={"reason": "测试手动 drain"}).json()
        assert drained["processed"] >= 1
        detail = client.get(f"/api/tg-accounts/{account['id']}/detail", headers=headers).json()
        assert detail["account"]["profile_sync_status"] == "已同步"
        assert detail["profile_sync_records"][0]["status"] == "已同步"

        retry = client.post(f"/api/tg-accounts/{account['id']}/profile-sync/retry", headers=headers).json()
        assert retry["status"] == "排队中"


def test_account_detail_reconciles_stale_sync_state_and_due_time():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _ = ensure_test_workspace(client, headers)
        old_time = (datetime.now(UTC) - timedelta(hours=7)).replace(tzinfo=None)

        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.profile_sync_status = "排队中"
            session.add(
                TgAccountSyncRecord(
                    tenant_id=1,
                    account_id=account["id"],
                    sync_type="health",
                    trigger_source="pytest",
                    status="已同步",
                    scheduled_at=old_time,
                    started_at=old_time,
                    finished_at=old_time,
                    created_at=old_time,
                )
            )
            session.add(
                TgAccountSyncRecord(
                    tenant_id=1,
                    account_id=account["id"],
                    sync_type="codes",
                    trigger_source="pytest",
                    status="同步中",
                    scheduled_at=old_time,
                    started_at=old_time,
                    created_at=old_time,
                )
            )
            session.add(
                TgAccountProfileSyncRecord(
                    tenant_id=1,
                    account_id=account["id"],
                    actor="pytest",
                    status="排队中",
                    created_at=old_time,
                )
            )
            session.commit()

        detail = client.get(f"/api/tg-accounts/{account['id']}/detail", headers=headers).json()
        assert detail["next_sync_at"] is None
        assert detail["sync_due"] is True
        assert "等待后台执行" in detail["sync_status_text"]
        assert any(record["status"] == "失败" and record["failure_type"] == "同步超时" for record in detail["sync_records"])
        assert detail["account"]["profile_sync_status"] == "失败"
        assert detail["profile_sync_records"][0]["status"] == "失败"
        assert detail["profile_sync_records"][0]["failure_type"] == "资料同步超时"
        removed = client.delete(f"/api/tg-accounts/{account['id']}", headers=headers)
        assert removed.status_code == 200, removed.text


def test_account_pool_clone_plan_and_verification_tasks():
    skip_legacy_task_center_flow()
    with TestClient(app) as client:
        headers = auth_headers(client)
        source, group = ensure_test_workspace(client, headers)
        pool = client.post(
            "/api/account-pools",
            headers=headers,
            json={"tenant_id": 1, "name": f"pytest账号池-{uuid4().hex[:6]}", "description": "pytest"},
        ).json()
        account = client.post(
            "/api/tg-accounts",
            headers=headers,
            json={"tenant_id": 1, "pool_id": pool["id"], "display_name": "池内账号", "phone_number": f"+86139{uuid4().int % 100000000:08d}"},
        ).json()
        assert account["pool_id"] == pool["id"]
        assert account["pool_name"] == pool["name"]
        pool_detail = client.get(f"/api/account-pools/{pool['id']}/detail", headers=headers).json()
        assert pool_detail["pool"]["id"] == pool["id"]
        assert any(item["id"] == account["id"] for item in pool_detail["accounts"])

        moved = client.post(f"/api/tg-accounts/{account['id']}/move-pool", headers=headers, json={"pool_id": pool["id"]}).json()
        assert moved["pool_id"] == pool["id"]
        filtered = client.get(f"/api/tg-accounts?pool_id={pool['id']}", headers=headers).json()
        assert any(item["id"] == account["id"] for item in filtered)

        client.post(f"/api/tg-accounts/{source['id']}/contacts/sync", headers=headers)
        second_target = client.post(
            "/api/tg-accounts",
            headers=headers,
            json={"tenant_id": 1, "pool_id": pool["id"], "display_name": "池内账号二", "phone_number": f"+86137{uuid4().int % 100000000:08d}"},
        ).json()
        plan = client.post(
            "/api/account-clone-plans",
            headers=headers,
            json={
                "tenant_id": 1,
                "source_account_id": source["id"],
                "target_account_ids": [account["id"], second_target["id"]],
                "clone_scope": ["contacts", "groups"],
            },
        ).json()
        assert plan["items_total"] > 0
        assert set(plan["target_account_ids"]) == {account["id"], second_target["id"]}
        assert set(plan["items_by_target"].keys()) == {str(account["id"]), str(second_target["id"])}
        legacy_plan = client.post(
            "/api/account-clone-plans",
            headers=headers,
            json={
                "tenant_id": 1,
                "source_account_id": source["id"],
                "target_account_id": account["id"],
                "clone_scope": ["contacts"],
            },
        ).json()
        assert legacy_plan["target_account_ids"] == [account["id"]]
        confirmed = client.post(f"/api/account-clone-plans/{plan['id']}/confirm", headers=headers).json()
        assert confirmed["status"] in {"已完成", "部分失败", "执行中"}
        pool_contacts = client.get(f"/api/account-pools/{pool['id']}/contacts", headers=headers).json()
        if pool_contacts:
            pool_task = client.post(
                f"/api/account-pools/{pool['id']}/direct-message-tasks",
                headers=headers,
                json={
                    "account_id": pool_contacts[0]["account_id"],
                    "target_peer_id": f"@{pool_contacts[0]['username']}" if pool_contacts[0]["username"] else pool_contacts[0]["peer_id"],
                    "target_display": pool_contacts[0]["display_name"],
                    "content": "pool hello",
                },
            ).json()
            assert pool_task["target_type"] == "private"

        with SessionLocal() as session:
            source_account = session.get(TgAccount, source["id"])
            source_account.status = AccountStatus.ACTIVE.value
            target_account = session.get(TgAccount, account["id"])
            target_account.status = AccountStatus.DISABLED.value
            session.commit()
        campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": group["id"],
                "title": "验证辅助测试",
                "campaign_type": "定时活跃任务",
                "topic": "验证辅助",
                "jitter_min_seconds": 0,
                "jitter_max_seconds": 0,
                "batch_interval_seconds": 0,
                "respect_send_window": False,
            },
        ).json()
        drafts = client.post(f"/api/campaigns/{campaign['id']}/generate-drafts", headers=headers, json={"count": 1}).json()
        task = client.post(f"/api/ai-drafts/{drafts[0]['id']}/approve", headers=headers, json={"actor": "测试操作员"}).json()
        with SessionLocal() as session:
            db_task = session.get(MessageTask, task["id"])
            db_task.preferred_account_id = account["id"]
            db_task.account_id = None
            session.commit()
        client.post(f"/api/groups/{group['id']}/authorize", headers=headers, json={"auth_status": "只读归档"})
        dispatched = client.post(f"/api/message-tasks/{task['id']}/dispatch", headers=headers).json()
        assert dispatched["status"] == "失败"
        verification_tasks = client.get("/api/verification-tasks", headers=headers).json()
        assert any(item["group_id"] == group["id"] for item in verification_tasks)
        with SessionLocal() as session:
            source_account = session.get(TgAccount, source["id"])
            source_account.status = AccountStatus.ACTIVE.value
            session.add(TgAccountSyncRecord(tenant_id=1, account_id=source["id"], sync_type="contacts", trigger_source="pytest", status="排队中", scheduled_at=source_account.created_at, created_at=source_account.created_at))
            session.commit()
        drained = client.post("/api/worker/drain-once", headers=headers, json={"reason": "测试手动 drain"}).json()
        assert drained["processed"] >= 1


def test_multi_group_recommendation_and_approval_expands_tasks():
    skip_legacy_task_center_flow()
    with TestClient(app) as client:
        headers = auth_headers(client)
        ensure_test_workspace(client, headers)
        groups = client.get("/api/groups", headers=headers).json()[:2]
        assert len(groups) >= 2
        for group in groups:
            client.post(f"/api/groups/{group['id']}/authorize", headers=headers, json={"auth_status": "已授权运营"})

        group_ids = [group["id"] for group in groups]
        with SessionLocal() as session:
            accounts = list(session.query(TgAccount).filter(TgAccount.tenant_id == 1).limit(2))
            for account in accounts:
                account.status = AccountStatus.ACTIVE.value
                account.health_score = 95
                for group_id in group_ids:
                    existing = session.query(TgGroupAccount).filter_by(group_id=group_id, account_id=account.id).first()
                    if not existing:
                        session.add(TgGroupAccount(tenant_id=1, group_id=group_id, account_id=account.id, can_send=True, permission_label="普通成员"))
            for link in session.query(TgGroupAccount).filter(TgGroupAccount.group_id.in_(group_ids)):
                link.can_send = True
            session.commit()

        recommendations = client.post(
            "/api/campaigns/recommend-accounts",
            headers=headers,
            json={"tenant_id": 1, "target_group_ids": group_ids},
        ).json()
        assert recommendations
        selected = {}
        for item in recommendations:
            assert "is_selectable" in item
            assert "cooldown_until" in item
            if item["is_selectable"] and item["recommended"]:
                selected.setdefault(str(item["group_id"]), []).append(item["account_id"])
        for group_id in group_ids:
            if not selected.get(str(group_id)):
                selected[str(group_id)] = [
                    item["account_id"]
                    for item in recommendations
                    if item["group_id"] == group_id and item["is_selectable"]
                ][:1]
        assert all(selected.get(str(group_id)) for group_id in group_ids)

        campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": group_ids[0],
                "title": "多群任务 pytest",
                "campaign_type": "多账号对话脚本",
                "topic": "多群同步讨论",
                "target_group_ids": group_ids,
                "selected_account_ids_by_group": selected,
                "jitter_min_seconds": 0,
                "jitter_max_seconds": 0,
                "batch_interval_seconds": 0,
                "respect_send_window": False,
            },
        ).json()
        assert campaign["target_group_ids"]
        drafts = client.post(f"/api/campaigns/{campaign['id']}/generate-drafts", headers=headers, json={"count": 3, "selected_account_ids_by_group": selected}).json()
        assert [draft["sequence_index"] for draft in drafts] == [1, 2, 3]
        assert all(draft["suggested_account_id"] in selected[str(group_ids[0])] for draft in drafts)
        tasks = client.post(f"/api/campaigns/{campaign['id']}/approve-all", headers=headers, json={"actor": "测试操作员"}).json()
        assert len(tasks) == len(group_ids) * 3
        assert {task["group_id"] for task in tasks} == set(group_ids)
        assert all(task["preferred_account_id"] in selected[str(task["group_id"])] for task in tasks)


def test_operation_targets_manual_send_and_task_lifecycle(monkeypatch):
    monkeypatch.setattr(
        "app.services.operations.gateway.send_message_to_target",
        lambda *args, **kwargs: SendResult(True, remote_message_id="operation-sent"),
    )
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _ = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            session.commit()

        group_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "group",
                "tg_peer_id": f"pytest-group-{uuid4().hex[:8]}",
                "title": "pytest 群目标",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-channel-{uuid4().hex[:8]}",
                "title": "pytest 频道目标",
                "username": "pytest_channel",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()

        manual = client.post(
            f"/api/tg-accounts/{account['id']}/manual-send",
            headers=headers,
            json={"target_id": group_target["id"], "content": "实时发送 emoji 😄"},
        )
        assert manual.status_code == 200, manual.text
        assert manual.json()["status"] == "已完成"
        assert manual.json()["remote_message_id"] == "operation-sent"

        message_task = client.post(
            "/api/operation-tasks",
            headers=headers,
            json={
                "task_type": "MESSAGE_SEND",
                "target_id": channel_target["id"],
                "title": "频道发帖任务",
                "content": "频道发送内容 😄",
                "account_ids": [account["id"]],
                "quantity": 1,
            },
        ).json()
        dispatched = client.post(f"/api/operation-tasks/{message_task['id']}/dispatch", headers=headers).json()
        assert dispatched["status"] == "已完成"
        assert dispatched["completed_count"] == 1

        channel_message = client.post(
            "/api/channel-messages",
            headers=headers,
            json={
                "channel_target_id": channel_target["id"],
                "message_id": 1001,
                "message_url": "https://t.me/pytest_channel/1001",
                "content_preview": "频道消息",
            },
        ).json()
        for task_type, payload in [
            ("CHANNEL_VIEW", {}),
            ("CHANNEL_REACTION", {"reaction": "👍"}),
            ("CHANNEL_REPLY", {"content": "评论区回复"}),
        ]:
            created = client.post(
                "/api/operation-tasks",
                headers=headers,
                json={
                    "task_type": task_type,
                    "channel_message_id": channel_message["id"],
                    "title": task_type,
                    "account_ids": [account["id"]],
                    "quantity": 1,
                    **payload,
                },
            )
            assert created.status_code == 200, created.text


def test_message_send_targets_are_scoped_to_selected_account():
    with TestClient(app) as client:
        headers = auth_headers(client)
        suffix = uuid4().hex[:8]
        with SessionLocal() as session:
            account_a = TgAccount(
                tenant_id=1,
                display_name=f"pytest 发送账号A {suffix}",
                username=f"pytest_send_a_{suffix}",
                phone_masked=f"+a-{suffix}",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="encrypted-session:pytest-a",
            )
            account_b = TgAccount(
                tenant_id=1,
                display_name=f"pytest 发送账号B {suffix}",
                username=f"pytest_send_b_{suffix}",
                phone_masked=f"+b-{suffix}",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="encrypted-session:pytest-b",
            )
            session.add_all([account_a, account_b])
            session.flush()
            account_a_id = account_a.id
            account_b_id = account_b.id

            target_ids: dict[str, int] = {}
            for owner, account, target_type, group_type in [
                ("a_group", account_a, "group", "supergroup"),
                ("a_channel", account_a, "channel", "channel"),
                ("b_group", account_b, "group", "supergroup"),
                ("b_channel", account_b, "channel", "channel"),
            ]:
                peer_id = f"pytest-{owner}-{suffix}"
                group = TgGroup(
                    tenant_id=1,
                    tg_peer_id=peer_id,
                    title=f"pytest {owner}",
                    group_type=group_type,
                    member_count=10,
                    auth_status="已授权运营",
                    can_send=True,
                )
                session.add(group)
                session.flush()
                target = OperationTarget(
                    tenant_id=1,
                    target_type=target_type,
                    tg_peer_id=peer_id,
                    title=group.title,
                    member_count=group.member_count,
                    can_send=True,
                    auth_status="已授权运营",
                )
                session.add(target)
                session.flush()
                session.add(
                    TgGroupAccount(
                        tenant_id=1,
                        group_id=group.id,
                        account_id=account.id,
                        can_send=True,
                        permission_label="普通成员",
                    )
                )
                target_ids[owner] = target.id
            session.commit()

        scoped_a = client.get(f"/api/operation-targets?account_id={account_a_id}", headers=headers)
        assert scoped_a.status_code == 200, scoped_a.text
        scoped_a_ids = {item["id"] for item in scoped_a.json()}
        assert scoped_a_ids >= {target_ids["a_group"], target_ids["a_channel"]}
        assert target_ids["b_group"] not in scoped_a_ids
        assert target_ids["b_channel"] not in scoped_a_ids

        scoped_b = client.get(f"/api/operation-targets?account_id={account_b_id}", headers=headers)
        assert scoped_b.status_code == 200, scoped_b.text
        scoped_b_ids = {item["id"] for item in scoped_b.json()}
        assert scoped_b_ids >= {target_ids["b_group"], target_ids["b_channel"]}
        assert target_ids["a_group"] not in scoped_b_ids
        assert target_ids["a_channel"] not in scoped_b_ids

        unscoped = client.get("/api/operation-targets", headers=headers)
        assert unscoped.status_code == 200, unscoped.text
        unscoped_ids = {item["id"] for item in unscoped.json()}
        assert {target_ids["a_group"], target_ids["a_channel"], target_ids["b_group"], target_ids["b_channel"]}.issubset(unscoped_ids)

        blocked = client.post(
            "/api/message-send-tasks/batch",
            headers=headers,
            json={
                "account_id": account_a_id,
                "targets": [{"target_type": "channel", "operation_target_id": target_ids["b_channel"]}],
                "content": "不应该跨账号发送",
                "message_type": "文本",
                "dispatch_now": False,
            },
        )
        assert blocked.status_code == 400
        assert "该账号不可向此运营目标发送" in blocked.text

        created = client.post(
            "/api/message-send-tasks/batch",
            headers=headers,
            json={
                "account_id": account_a_id,
                "targets": [
                    {"target_type": "group", "operation_target_id": target_ids["a_group"]},
                    {"target_type": "channel", "operation_target_id": target_ids["a_channel"]},
                ],
                "content": "账号自己的目标可以创建",
                "message_type": "文本",
                "dispatch_now": False,
            },
        )
        assert created.status_code == 200, created.text
        assert [item["account_id"] for item in created.json()] == [account_a_id, account_a_id]


def test_operation_target_detail_returns_group_context_messages():
    with TestClient(app) as client:
        headers = auth_headers(client)
        with SessionLocal() as session:
            suffix = uuid4().hex[:8]
            account = TgAccount(
                tenant_id=1,
                display_name="pytest 详情账号",
                username=f"pytest_detail_{suffix}",
                phone_masked=f"+detail-{suffix}",
                status=AccountStatus.ACTIVE.value,
                health_score=98,
                session_ciphertext="encrypted-session:pytest",
            )
            group = TgGroup(
                tenant_id=1,
                tg_peer_id=f"pytest-detail-group-{suffix}",
                title="pytest 目标详情群",
                group_type="supergroup",
                member_count=12,
                auth_status="已授权运营",
                can_send=True,
                listener_enabled=True,
            )
            session.add_all([account, group])
            session.flush()
            target = OperationTarget(
                tenant_id=1,
                target_type="group",
                tg_peer_id=group.tg_peer_id,
                title=group.title,
                member_count=group.member_count,
                can_send=group.can_send,
                auth_status=group.auth_status,
            )
            session.add(target)
            session.flush()
            session.add(
                TgGroupAccount(
                    tenant_id=1,
                    group_id=group.id,
                    account_id=account.id,
                    permission_label="监听成员",
                    can_send=True,
                    is_listener=True,
                )
            )
            session.add(
                GroupContextMessage(
                    tenant_id=1,
                    group_id=group.id,
                    listener_account_id=account.id,
                    sender_name="pytest 用户",
                    content="这是一条目标详情里的群聊记录",
                    message_type="text",
                    remote_message_id=f"pytest-detail-{uuid4().hex[:8]}",
                    sent_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            session.commit()
            target_id = target.id
            group_id = group.id
            account_id = account.id

        detail = client.get(f"/api/operation-targets/{target_id}/detail", headers=headers)
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["linked_group"]["id"] == group_id
        assert body["target"]["can_task"] is True
        assert body["target"]["can_archive"] is True
        assert {"AI 活跃群", "转发监听源群", "转发目标群", "群归档"}.issubset(set(body["target"]["task_capabilities"]))
        assert body["accounts"]
        assert body["stats"]["listener_accounts"] >= 1
        assert any(message["content"] == "这是一条目标详情里的群聊记录" for message in body["group_messages"])

        targets = client.get("/api/operation-targets", headers=headers)
        assert targets.status_code == 200, targets.text
        listed = next(item for item in targets.json() if item["id"] == target_id)
        assert listed["task_capabilities"] == body["target"]["task_capabilities"]

        patched = client.patch(
            f"/api/operation-targets/{target_id}/accounts/{account_id}",
            headers=headers,
            json={"can_send": False, "is_listener": False, "permission_label": "风控观察"},
        )
        assert patched.status_code == 200, patched.text
        patched_body = patched.json()
        patched_account = next(item for item in patched_body["accounts"] if item["id"] == account_id)
        assert patched_account["can_send"] is False
        assert patched_account["is_listener"] is False
        assert patched_account["permission_label"] == "风控观察"


def test_operation_target_admission_retry_endpoint_queues_actions_and_audit(monkeypatch):
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("admission retry endpoint must not call Telegram directly")

    monkeypatch.setattr("app.services.operations.gateway.list_groups", fail_if_called)

    with TestClient(app) as client:
        headers = auth_headers(client)
        with SessionLocal() as session:
            suffix = uuid4().hex[:8]
            account = TgAccount(
                tenant_id=1,
                display_name="pytest 准入账号",
                username=f"pytest_admission_{suffix}",
                phone_masked=f"+admission-{suffix}",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="encrypted-session:pytest",
            )
            group = TgGroup(
                tenant_id=1,
                tg_peer_id=f"pytest-admission-group-{suffix}",
                title="pytest 准入群",
                group_type="supergroup",
                member_count=18,
                auth_status="只读",
                can_send=False,
            )
            session.add_all([account, group])
            session.flush()
            target = OperationTarget(
                tenant_id=1,
                target_type="group",
                tg_peer_id=group.tg_peer_id,
                title=group.title,
                member_count=group.member_count,
                can_send=False,
                auth_status="只读",
            )
            session.add(target)
            session.flush()
            session.add(TgGroupAccount(tenant_id=1, group_id=group.id, account_id=account.id, permission_label="禁言", can_send=False))
            session.commit()
            target_id = target.id
            account_id = account.id

        before = client.get(f"/api/operation-targets/{target_id}/detail", headers=headers)
        assert before.status_code == 200, before.text
        before_account = next(item for item in before.json()["accounts"] if item["id"] == account_id)
        assert before_account["admission_status"] == "failed"

        blank_reason = client.post(
            f"/api/operation-targets/{target_id}/admission/retry",
            headers=headers,
            json={"reason": "   ", "account_ids": [account_id]},
        )
        assert blank_reason.status_code == 422, blank_reason.text
        assert "重试原因不能为空" in blank_reason.text

        retried = client.post(
            f"/api/operation-targets/{target_id}/admission/retry",
            headers=headers,
            json={"reason": "管理员已解除限制", "account_ids": [account_id]},
        )
        assert retried.status_code == 200, retried.text
        body = retried.json()
        retried_account = next(item for item in body["accounts"] if item["id"] == account_id)

        with SessionLocal() as session:
            audit_row = session.query(AuditLog).filter(AuditLog.action == "重试目标准入", AuditLog.target_id == str(target_id)).order_by(AuditLog.id.desc()).first()
            queued_action = session.scalar(select(Action).where(Action.action_type == "ensure_target_membership", Action.account_id == account_id))

    assert body["admission_retry"]["mode"] == "queued"
    assert body["admission_retry"]["queued_action_count"] == 1
    assert body["admission_retry"]["recovered_account_count"] == 0
    assert body["target"]["can_send"] is False
    assert retried_account["admission_status"] == "failed"
    assert queued_action is not None
    assert queued_action.status == "pending"
    assert audit_row is not None
    assert "reason=管理员已解除限制" in audit_row.detail
    assert "queued=1" in audit_row.detail


def test_operation_target_sync_messages_collects_channel_messages(monkeypatch):
    def fake_fetch_channel_messages(*args, **kwargs):
        return [
            ChannelMessageSnapshot(
                message_id=5101,
                content_preview="目标详情自动同步频道消息",
                message_url="https://t.me/pytest_target_detail/5101",
                published_at=datetime.now(UTC).replace(tzinfo=None),
            )
        ]

    monkeypatch.setattr("app.services.operations.gateway.fetch_channel_messages", fake_fetch_channel_messages)
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            session.commit()

        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-target-detail-channel-{uuid4().hex[:8]}",
                "title": "pytest 目标详情频道",
                "username": "pytest_target_detail",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()

        synced = client.post(f"/api/operation-targets/{channel_target['id']}/sync-messages", headers=headers)
        assert synced.status_code == 200, synced.text
        body = synced.json()
        assert body["inserted"] == 1
        assert body["detail"]["sync_error"] == ""
        assert body["detail"]["channel_messages"][0]["message_id"] == 5101

        detail = client.get(f"/api/operation-targets/{channel_target['id']}/detail", headers=headers).json()
        assert detail["stats"]["channel_messages"] >= 1


def test_channel_message_sync_comments_collects_reply_targets(monkeypatch):
    def fake_fetch_channel_messages(*args, **kwargs):
        return [
            ChannelMessageSnapshot(
                message_id=6101,
                content_preview="需要采集评论的频道消息",
                message_url="https://t.me/pytest_comment_tree/6101",
                published_at=datetime.now(UTC).replace(tzinfo=None),
            )
        ]

    def fake_fetch_channel_comments(*args, **kwargs):
        return [
            ChannelCommentSnapshot(
                comment_message_id=9001,
                parent_comment_message_id=6101,
                author_peer_id="pytest-author",
                author_name="评论用户",
                content_preview="这个频道消息下面的一级评论",
                reply_count=2,
                published_at=datetime.now(UTC).replace(tzinfo=None),
            ),
            ChannelCommentSnapshot(
                comment_message_id=9002,
                parent_comment_message_id=9001,
                author_peer_id="pytest-replier",
                author_name="回复用户",
                content_preview="这个频道消息下面的二级回复",
                reply_count=0,
                published_at=datetime.now(UTC).replace(tzinfo=None),
            ),
        ]

    monkeypatch.setattr("app.services.operations.gateway.fetch_channel_messages", fake_fetch_channel_messages)
    monkeypatch.setattr("app.services.operations.gateway.fetch_channel_comments", fake_fetch_channel_comments)
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            session.commit()

        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-comment-tree-{uuid4().hex[:8]}",
                "title": "pytest 评论树频道",
                "username": "pytest_comment_tree",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        synced_messages = client.post(f"/api/operation-targets/{channel_target['id']}/sync-messages", headers=headers).json()
        channel_message = synced_messages["detail"]["channel_messages"][0]

        synced_comments = client.post(f"/api/channel-messages/{channel_message['id']}/sync-comments", headers=headers)
        assert synced_comments.status_code == 200, synced_comments.text
        body = synced_comments.json()
        assert body["inserted"] == 2
        assert body["sync_error"] == ""
        assert {item["comment_message_id"] for item in body["comments"]} == {9001, 9002}
        assert next(item for item in body["comments"] if item["comment_message_id"] == 9001)["parent_comment_message_id"] is None
        assert next(item for item in body["comments"] if item["comment_message_id"] == 9002)["parent_comment_message_id"] == 9001

        comments = client.get(f"/api/channel-comments?channel_message_id={channel_message['id']}", headers=headers)
        assert comments.status_code == 200, comments.text
        assert len(comments.json()) == 2

        detail = client.get(f"/api/operation-targets/{channel_target['id']}/detail", headers=headers).json()
        assert detail["stats"]["channel_comments"] == 2


def test_operation_target_sync_messages_reports_missing_collect_account(monkeypatch):
    monkeypatch.setattr("app.services.operations._channel_sync_account", lambda *args, **kwargs: None)
    with TestClient(app) as client:
        headers = auth_headers(client)
        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-target-no-account-{uuid4().hex[:8]}",
                "title": "pytest 无采集账号目标",
                "username": "pytest_no_collect",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        synced = client.post(f"/api/operation-targets/{channel_target['id']}/sync-messages", headers=headers)
        assert synced.status_code == 200, synced.text
        body = synced.json()
        assert body["inserted"] == 0
        assert "没有可用于采集频道消息的在线账号" in body["detail"]["sync_error"]
        assert body["detail"]["channel_messages"] == []


def test_task_center_group_ai_chat_creates_and_dispatches_actions(monkeypatch):
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_message",
        lambda *args, **kwargs: SendResult(True, remote_message_id="task-center-sent"),
    )
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        group = make_isolated_ai_group(account["id"], "pytest AI 活跃创建")
        enable_mock_ai_provider(client, headers, "pytest AI 活跃创建")
        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            session.commit()
        created = client.post(
            "/api/tasks/group-ai-chat",
            headers=headers,
            json={
                "name": "5类型 AI 活跃",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": workflow_ai_active_pacing(),
                "failure_policy": {"max_retries": 1, "retry_delay_seconds": 0, "retry_backoff": "none"},
                "target_group_id": group["id"],
                "topic_directions": [{"title": "测试话题", "weight": 1}],
                "participation_rate": 1,
                "participation_jitter": 0,
                "messages_per_round_mode": "manual",
                "messages_per_round": 1,
            },
        )
        assert created.status_code == 200, created.text
        task = created.json()
        assert task["status"] == "draft"

        started = client.post(f"/api/tasks/{task['id']}/start", headers=headers)
        assert started.status_code == 200, started.text
        assert started.json()["status"] == "running"

        drained = client.post("/api/worker/drain-once", headers=headers, json={"reason": "测试手动 drain"}).json()
        assert drained["processed"] >= 1
        make_task_send_actions_due(task["id"])
        drained = client.post("/api/worker/drain-once", headers=headers, json={"reason": "测试发送 drain"}).json()
        assert drained["processed"] >= 1
        detail = client.get(f"/api/tasks/{task['id']}", headers=headers).json()
        assert detail["task"]["stats"]["total_actions"] >= 1
        assert detail["task"]["stats"]["success_count"] >= 1
        actions = task_detail_actions(client, headers, task["id"] if isinstance(task, dict) else task_id)
        assert actions[0]["action_type"] == "send_message"
        client.post(f"/api/tasks/{task['id']}/stop", headers=headers, json={"reason": "测试停止任务"})


def test_task_center_group_ai_chat_runs_from_worker_loop(monkeypatch):
    from app import worker

    monkeypatch.setattr(worker.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_message",
        lambda *args, **kwargs: SendResult(True, remote_message_id="worker-loop-sent"),
    )
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        group = make_isolated_ai_group(account["id"], "pytest AI worker")
        enable_mock_ai_provider(client, headers, "pytest AI 活跃 worker")
        created = client.post(
            "/api/tasks/group-ai-chat",
            headers=headers,
            json={
                "name": "worker loop AI 活跃",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": workflow_ai_active_pacing(),
                "failure_policy": {"max_retries": 1, "retry_delay_seconds": 0, "retry_backoff": "none"},
                "target_group_id": group["id"],
                "topic_directions": [{"title": "worker loop 测试", "weight": 1}],
                "participation_rate": 1,
                "participation_jitter": 0,
                "messages_per_round": 1,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        started = client.post(f"/api/tasks/{task_id}/start", headers=headers)
        assert started.status_code == 200, started.text

        worker.run_worker(limit=1000, interval_seconds=0.1, max_iterations=1)
        make_task_send_actions_due(task_id)
        worker.run_worker(limit=1000, interval_seconds=0.1, max_iterations=2)

        detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        assert detail["task"]["status"] == "running"
        actions = task_detail_actions(client, headers, task_id, action_type="send_message")
        assert detail["task"]["stats"]["success_count"] >= 1, {
            "task": compact_task_debug(detail["task"]),
            "actions": compact_action_debug(actions),
        }
        assert actions[0]["action_type"] == "send_message"


def test_task_center_group_ai_chat_cycles_and_picks_up_new_context(monkeypatch):
    context_suffix = uuid4().hex[:8]
    second_context_marker = f"second-cycle-{context_suffix}"
    messages = [
        (f"ai-context-1-{context_suffix}", f"第一条真人上下文 {context_suffix}"),
    ]
    sends: list[str] = []

    def fake_fetch_group_messages(*args, **kwargs):
        return [
            GroupMessageSnapshot(
                remote_message_id=remote_id,
                sender_peer_id="pytest-real-user",
                sender_name="真人用户",
                content=content,
                sent_at=datetime.now(UTC).replace(tzinfo=None),
            )
            for remote_id, content in messages
        ]

    def fake_generate_drafts(_credentials, prompt, **_kwargs):
        if f"第二条真人上下文 {context_suffix}" in prompt:
            base_candidates = [
                ("自然群友", f"第二条真人上下文 {context_suffix} {second_context_marker} 这个信息可以往具体案例上聊。"),
                ("补充群友", f"这个新内容 {context_suffix} {second_context_marker} 更适合先问问实际发生了什么。"),
            ]
        else:
            base_candidates = [
                ("自然群友", f"第一条真人上下文 {context_suffix} 可以先从实际体验聊起。"),
                ("补充群友", f"我觉得第一条真人上下文 {context_suffix} 这里要看具体情况。"),
            ]
        count = int(_kwargs.get("count") or len(base_candidates))
        candidates = []
        for index in range(count):
            persona, content = base_candidates[index % len(base_candidates)]
            token = _workflow_ai_token(index)
            candidates.append(AiDraftCandidate(persona=persona, content=f"pytest-{token} {content}"))
        return AiGenerationResult(
            candidates=candidates,
            usage=AiUsage(total_tokens=18),
        )

    monkeypatch.setattr("app.services.group_listeners.gateway.fetch_group_messages", fake_fetch_group_messages)
    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_message",
        lambda *args, **kwargs: sends.append(args[2]) or SendResult(True, remote_message_id=f"ai-continuous-{len(sends)}"),
    )
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        group = make_isolated_ai_group(account["id"], "pytest AI 持续监听")
        provider = client.post(
            "/api/ai-providers",
            headers=headers,
            json={
                "provider_name": "pytest AI 活跃",
                "provider_type": "openai_compatible",
                "base_url": "mock://group-ai",
                "model_name": "mino-v2.5",
                "api_key": "pytest",
                "api_key_header": "Authorization",
            },
        ).json()
        client.patch(
            "/api/tenant-ai-settings?tenant_id=1",
            headers=headers,
            json={"default_provider_id": provider["id"], "ai_enabled": True, "fallback_to_mock": False, "temperature": 0.8, "max_tokens": 512},
        )
        created = client.post(
            "/api/tasks/group-ai-chat/create-and-start",
            headers=headers,
            json={
                "name": "pytest AI 持续监听",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": workflow_ai_active_pacing(),
                "target_group_id": group["id"],
                "topic_directions": [{"title": "continuous ai", "weight": 1}],
                "participation_rate": 1,
                "participation_jitter": 0,
                "messages_per_round_mode": "manual",
                "messages_per_round": 1,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]

        from app.services.task_center.service import drain_task_center

        drain_task_center(SessionLocal, 10)
        make_task_send_actions_due(task_id)
        drain_task_center(SessionLocal, 10)
        with SessionLocal() as session:
            for action in session.scalars(
                select(Action).where(
                    Action.task_id == task_id,
                    Action.action_type == "send_message",
                    Action.status.in_(["pending", "claiming", "executing"]),
                )
            ):
                action.status = "skipped"
                action.executed_at = datetime.now(UTC).replace(tzinfo=None)
                action.result = {"success": False, "error_code": "test_cycle_boundary", "error_message": "test cycle boundary"}
            session.commit()
        first_context_send_count = len(sends)
        actions = task_detail_actions(client, headers, task_id, action_type="send_message")
        detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        assert first_context_send_count >= 1, {
            "task": compact_task_debug(detail["task"]),
            "actions": compact_action_debug(actions),
            "sends": sends,
        }

        messages.append((f"ai-context-2-{context_suffix}", f"第二条真人上下文 {context_suffix}"))
        from app.services.group_listeners import collect_group_context

        with SessionLocal() as session:
            db_group = session.get(TgGroup, group["id"])
            assert db_group is not None
            inserted = collect_group_context(session, db_group, [account["id"]])
            task = session.get(Task, task_id)
            assert task is not None
            task.next_run_at = datetime.now(UTC).replace(tzinfo=None)
            session.commit()
        assert inserted >= 1

        drain_task_center(SessionLocal, 10)
        make_task_send_actions_due(task_id)
        drain_task_center(SessionLocal, 10)
        detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        if len(sends) <= first_context_send_count:
            make_task_send_actions_due(task_id)
            drain_task_center(SessionLocal, 10)
            detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        assert len(sends) > first_context_send_count
        second_cycle_sends = sends[first_context_send_count:]
        assert any(second_context_marker in content for content in second_cycle_sends), second_cycle_sends
        assert detail["task"]["status"] == "running"
        assert detail["task"]["stats"]["success_count"] >= len(sends)


def test_legacy_task_write_apis_remain_compatible():
    with TestClient(app) as client:
        headers = auth_headers(client)
        operation = client.get("/api/operation-tasks", headers=headers)
        assert operation.status_code == 200
        assert client.get("/api/v2/tasks", headers=headers).status_code == 404
        assert client.get("/api/campaigns", headers=headers).status_code == 200
        assert client.get("/api/campaigns/1/detail", headers=headers).status_code in {200, 404}
        campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={"tenant_id": 1, "group_id": 1, "title": "旧任务", "campaign_type": "定时活跃任务", "topic": "旧任务"},
        )
        assert campaign.status_code != 410


def test_task_center_group_ai_chat_does_not_plan_over_open_actions(monkeypatch):
    from app.services.task_center.service import drain_task_center

    def unexpected_build_task_plan(*args, **kwargs):
        raise AssertionError("open actions should block a new AI chat round")

    monkeypatch.setattr("app.services.task_center.service.build_task_plan", unexpected_build_task_plan)
    with TestClient(app):
        future = datetime.now(UTC) + timedelta(hours=1)
        with SessionLocal() as session:
            task = Task(
                tenant_id=1,
                name="pytest open action guard",
                type="group_ai_chat",
                status="running",
                next_run_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1),
                account_config={},
                pacing_config={"mode": "fixed", "interval_seconds_min": 60},
                failure_policy={},
                type_config={},
                stats={},
            )
            session.add(task)
            session.flush()
            session.add(
                Action(
                    tenant_id=1,
                    task_id=task.id,
                    task_type=task.type,
                    action_type="send_message",
                    account_id=None,
                    scheduled_at=future,
                    status="pending",
                    payload={"message_text": "还没发完"},
                    result={},
                )
            )
            session.commit()
            task_id = task.id

        assert drain_task_center(SessionLocal, 10) == 0
        with SessionLocal() as session:
            task = session.get(Task, task_id)
            assert task.next_run_at.replace(tzinfo=None) == future.replace(tzinfo=None)
            assert session.query(Action).filter(Action.task_id == task_id).count() == 1


def test_relay_filter_only_with_media_blocks_text_messages():
    from app.services.task_center.executors.group_relay import passes_relay_filters

    assert not passes_relay_filters("纯文本", "user-1", "text", {"only_with_media": True})
    assert not passes_relay_filters("纯文本", "user-1", "文本", {"only_with_media": True})
    assert passes_relay_filters("带图消息", "user-1", "photo", {"only_with_media": True})


def test_task_center_channel_view_like_comment_execute(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.view_channel_message",
        lambda *args, **kwargs: calls.append("view") or OperationResult(True, detail="viewed"),
    )
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_channel_reaction",
        lambda *args, **kwargs: calls.append("like") or OperationResult(True, detail="liked"),
    )
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.reply_channel_message",
        lambda *args, **kwargs: calls.append("comment") or SendResult(True, remote_message_id="comment-sent"),
    )
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            session.commit()
        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-channel-{uuid4().hex[:8]}",
                "title": "pytest 频道增长目标",
                "username": "pytest_growth",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        mark_test_channel_comment_ready(channel_target["id"], [account["id"]])
        channel_message = client.post(
            "/api/channel-messages",
            headers=headers,
            json={
                "channel_target_id": channel_target["id"],
                "message_id": 2001,
                "message_url": "https://t.me/pytest_growth/2001",
                "content_preview": "频道增长消息",
            },
        ).json()
        task_payloads = [
            ("channel_view", "/api/tasks/channel-view", {"target_views_per_message": 1, "view_count_jitter": 0}),
            ("channel_like", "/api/tasks/channel-like", {"target_likes_per_message": 1, "like_count_jitter": 0, "allowed_reactions": ["👍"]}),
            ("channel_comment", "/api/tasks/channel-comment", {"target_comments_per_message": 1, "comment_count_jitter": 0, "topic_hint": "评论测试"}),
        ]
        task_ids: list[str] = []
        for task_type, endpoint, extra_config in task_payloads:
            created = client.post(
                endpoint,
                headers=headers,
                json={
                    "name": f"pytest {task_type}",
                    "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                    "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                    "failure_policy": {"max_retries": 1, "retry_delay_seconds": 0, "retry_backoff": "none"},
                    "target_channel_id": channel_target["id"],
                    "message_scope": "specific",
                    "message_ids": [channel_message["id"]],
                    **extra_config,
                },
            )
            assert created.status_code == 200, created.text
            task_ids.append(created.json()["id"])
            client.post(f"/api/tasks/{task_ids[-1]}/start", headers=headers)

        processed = 0
        for _ in range(5):
            processed += client.post("/api/worker/drain-once", headers=headers, json={"reason": "测试手动 drain"}).json()["processed"]
            if len(calls) >= 3:
                break
        assert processed >= 3
        assert sorted(calls) == ["comment", "like", "view"]
        for task_id in task_ids:
            detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
            assert detail["task"]["status"] == "running"
            assert detail["task"]["stats"]["success_count"] >= 1
            actions = task_detail_actions(client, headers, task_id)
            message_groups = task_detail_message_groups(client, headers, task_id)
            assert actions[0]["account_display_name"] == account["display_name"]
            assert message_groups[0]["channel_title"] == "pytest 频道增长目标"
            assert message_groups[0]["content_preview"] == "频道增长消息"
            assert message_groups[0]["stats"]["success"] >= 1
        listed = client.get("/api/tasks", headers=headers).json()
        listed_channel_tasks = [item for item in listed if item["id"] in task_ids]
        assert listed_channel_tasks
        assert any("pytest 频道增长目标" in item["search_text"] and "频道增长消息" in item["search_text"] for item in listed_channel_tasks)

        removed = client.request("DELETE", f"/api/tasks/{task_ids[0]}", headers=headers, json={"reason": "测试删除任务"})
        assert removed.status_code == 204, removed.text
        visible_tasks = client.get("/api/tasks", headers=headers).json()
        assert task_ids[0] not in {item["id"] for item in visible_tasks}
        assert client.get(f"/api/tasks/{task_ids[0]}", headers=headers).status_code == 404
        with SessionLocal() as session:
            assert session.query(Action).filter(Action.task_id == task_ids[0]).count() >= 1
            assert session.get(Task, task_ids[0]).deleted_at is not None


def test_task_center_channel_comment_capacity_check_accepts_comment_task_type():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        capacity = client.post(
            "/api/tasks/channel-capacity-check",
            headers=headers,
            json={
                "task_type": "channel_comment",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "target_per_message": 2,
                "message_scope": "latest_n",
                "message_count": 1,
            },
        )

        assert capacity.status_code == 200, capacity.text
        body = capacity.json()
        assert body["target_per_message"] == 2
        assert body["max_effective_per_message"] == 1
        assert "目标评论 2" in body["warning_message"]


def test_task_center_channel_like_and_view_cap_per_message_by_unique_accounts(monkeypatch):
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.view_channel_message",
        lambda *args, **kwargs: OperationResult(True, detail="viewed"),
    )
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_channel_reaction",
        lambda *args, **kwargs: OperationResult(True, detail="liked"),
    )
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            first = session.get(TgAccount, account["id"])
            first.status = AccountStatus.ACTIVE.value
            second = TgAccount(
                tenant_id=1,
                display_name="pytest 第二点赞账号",
                username=f"pytest_like_second_{uuid4().hex[:8]}",
                phone_masked=f"+like-second-{uuid4().hex[:8]}",
                status=AccountStatus.ACTIVE.value,
                health_score=97,
                session_ciphertext=first.session_ciphertext,
                developer_app_id=first.developer_app_id,
                developer_app_version=first.developer_app_version,
            )
            session.add(second)
            session.commit()
            account_ids = [first.id, second.id]

        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-cap-channel-{uuid4().hex[:8]}",
                "title": "pytest 容量频道",
                "username": "pytest_capacity_channel",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        channel_message = client.post(
            "/api/channel-messages",
            headers=headers,
            json={
                "channel_target_id": channel_target["id"],
                "message_id": 6101,
                "message_url": "https://t.me/pytest_capacity_channel/6101",
                "content_preview": "容量去重消息",
            },
        ).json()

        capacity = client.post(
            "/api/tasks/channel-capacity-check",
            headers=headers,
            json={
                "task_type": "channel_like",
                "account_config": {"selection_mode": "manual", "account_ids": account_ids, "max_concurrent": 2, "cooldown_per_account_minutes": 0},
                "target_per_message": 50,
                "target_channel_id": channel_target["id"],
                "target_channel_name": channel_target["title"],
                "message_scope": "specific",
                "date_from": None,
                "date_to": None,
                "message_ids": [channel_message["id"]],
            },
        )
        assert capacity.status_code == 200, capacity.text
        assert capacity.json()["will_shortfall"] is True
        assert capacity.json()["max_effective_per_message"] == 2

        task_ids: list[str] = []
        for endpoint, extra_config in [
            ("/api/tasks/channel-like", {"target_likes_per_message": 50, "like_count_jitter": 0, "allowed_reactions": ["👍"], "max_likes_per_account_per_hour": 999}),
            ("/api/tasks/channel-view", {"target_views_per_message": 50, "view_count_jitter": 0}),
        ]:
            created = client.post(
                endpoint,
                headers=headers,
                json={
                    "name": f"pytest capacity {endpoint}",
                    "account_config": {"selection_mode": "manual", "account_ids": account_ids, "max_concurrent": 2, "cooldown_per_account_minutes": 0},
                    "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                    "target_channel_id": channel_target["id"],
                    "message_scope": "specific",
                    "message_ids": [channel_message["id"]],
                    **extra_config,
                },
            )
            assert created.status_code == 200, created.text
            task_id = created.json()["id"]
            task_ids.append(task_id)
            client.post(f"/api/tasks/{task_id}/start", headers=headers)
            client.post("/api/worker/drain-once", headers=headers, json={"reason": "测试手动 drain"})
            client.post("/api/worker/drain-once", headers=headers, json={"reason": "测试手动 drain"})
            detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
            rows = task_detail_actions(client, headers, task_id)
            assert len(rows) == 2
            assert len({row["account_id"] for row in rows}) == 2
            assert "当前参与账号 2 个" in detail["task"]["stats"]["capacity_warning"]
            assert detail["task"]["status"] == "running"
            assert detail["task"]["stats"]["total_actions"] == 2

        from app.services.task_center.service import drain_task_center

        drain_task_center(SessionLocal, 10)
        with SessionLocal() as session:
            for task_id in task_ids:
                assert session.query(Action).filter(Action.task_id == task_id).count() == 2


def test_task_center_channel_comment_allows_multiple_replies_per_account(monkeypatch):
    replies: list[tuple[int, str]] = []
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.reply_channel_message",
        lambda *args, **kwargs: replies.append((args[2], args[3])) or SendResult(True, remote_message_id=f"reply-{len(replies)}"),
    )
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            first = session.get(TgAccount, account["id"])
            first.status = AccountStatus.ACTIVE.value
            first.username = first.username or f"pytest_comment_first_{uuid4().hex[:8]}"
            first.tg_first_name = "评论号一"
            first.avatar_object_key = first.avatar_object_key or "avatars/pytest-comment-first.jpg"
            first.profile_sync_status = "已同步"
            second = TgAccount(
                tenant_id=1,
                display_name="pytest 第二评论账号",
                username=f"pytest_comment_second_{uuid4().hex[:8]}",
                tg_first_name="评论号二",
                avatar_object_key="avatars/pytest-comment-second.jpg",
                profile_sync_status="已同步",
                phone_masked=f"+comment-second-{uuid4().hex[:8]}",
                status=AccountStatus.ACTIVE.value,
                health_score=97,
                session_ciphertext=first.session_ciphertext,
                developer_app_id=first.developer_app_id,
                developer_app_version=first.developer_app_version,
            )
            session.add(second)
            session.commit()
            account_ids = [first.id, second.id]
        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-comment-cap-channel-{uuid4().hex[:8]}",
                "title": "pytest 评论容量频道",
                "username": "pytest_comment_capacity",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        channel_message = client.post(
            "/api/channel-messages",
            headers=headers,
            json={
                "channel_target_id": channel_target["id"],
                "message_id": 6201,
                "message_url": "https://t.me/pytest_comment_capacity/6201",
                "content_preview": "评论可以多条",
            },
        ).json()
        created = client.post(
            "/api/tasks/channel-comment",
            headers=headers,
            json={
                "name": "pytest comments can repeat accounts",
                "account_config": {"selection_mode": "manual", "account_ids": account_ids, "max_concurrent": 2, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                "target_channel_id": channel_target["id"],
                "message_scope": "specific",
                "message_ids": [channel_message["id"]],
                "target_comments_per_message": 3,
                "comment_count_jitter": 0,
                "topic_hint": "评论可以多条",
                "max_comments_per_account_per_hour": 500,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        client.post(f"/api/tasks/{task_id}/start", headers=headers)
        from app.services.task_center.service import drain_task_center

        drain_task_center(SessionLocal, 10)
        from app.services.task_center.service import drain_task_center

        drain_task_center(SessionLocal, 10)
        detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        actions = task_detail_actions(client, headers, task_id, action_type="post_comment")
        assert len(actions) == 3
        assert detail["task"]["stats"]["total_actions"] == 3


def test_task_center_channel_like_auto_collects_dynamic_new_messages(monkeypatch):
    calls: list[int] = []

    def fake_fetch_channel_messages(*args, **kwargs):
        return [
            ChannelMessageSnapshot(
                message_id=4101,
                content_preview="自动采集频道消息",
                message_url="https://t.me/pytest_auto_channel/4101",
                published_at=datetime.now(UTC).replace(tzinfo=None),
            )
        ]

    monkeypatch.setattr("app.services.task_center.executors.common.gateway.fetch_channel_messages", fake_fetch_channel_messages)
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_channel_reaction",
        lambda *args, **kwargs: calls.append(args[2]) or OperationResult(True, detail="liked"),
    )
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-auto-channel-{uuid4().hex[:8]}",
                "title": "pytest 自动采集频道",
                "username": "pytest_auto_channel",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        created = client.post(
            "/api/tasks/channel-like",
            headers=headers,
            json={
                "name": "pytest dynamic collect like",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                "target_channel_id": channel_target["id"],
                "message_scope": "dynamic_new",
                "message_count": 1,
                "target_likes_per_message": 1,
                "like_count_jitter": 0,
                "allowed_reactions": ["👍"],
                "max_likes_per_account_per_hour": 999,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        started = client.post(f"/api/tasks/{task_id}/start", headers=headers)
        assert started.status_code == 200, started.text
        from app.services.task_center.service import drain_task_center

        first_processed = drain_task_center(SessionLocal, 10)
        assert first_processed >= 1, client.get(f"/api/tasks/{task_id}", headers=headers).text
        if not calls:
            assert drain_task_center(SessionLocal, 10) >= 1

        assert calls == [4101]
        fetched_ids = [4102]
        monkeypatch.setattr(
            "app.services.task_center.executors.common.gateway.fetch_channel_messages",
            lambda *args, **kwargs: [
                ChannelMessageSnapshot(
                    message_id=fetched_ids[-1],
                    content_preview=f"持续采集频道消息 {fetched_ids[-1]}",
                    message_url=f"https://t.me/pytest_auto_channel/{fetched_ids[-1]}",
                    published_at=datetime.now(UTC).replace(tzinfo=None),
                )
            ],
        )
        reset_listener_runtime_cache()
        with SessionLocal() as session:
            task = session.get(Task, task_id)
            task.next_run_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
            session.commit()
        assert drain_task_center(SessionLocal, 10) >= 1
        if calls == [4101]:
            assert drain_task_center(SessionLocal, 10) >= 1
        assert calls == [4101, 4102]
        messages = client.get(f"/api/channel-messages?channel_target_id={channel_target['id']}", headers=headers).json()
        assert {message["message_id"] for message in messages} >= {4101, 4102}
        detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        assert detail["task"]["status"] == "running"
        assert detail["task"]["last_error"] == ""


@pytest.mark.parametrize(
    ("endpoint", "action_type", "payload_extra", "gateway_attr", "result_factory"),
    [
        (
            "/api/tasks/channel-view/create-and-start",
            "view_message",
            {"target_views_per_message": 1, "view_count_jitter": 0},
            "view_channel_message",
            lambda calls: (lambda *args, **kwargs: calls.append(args[2]) or OperationResult(True, detail="viewed")),
        ),
        (
            "/api/tasks/channel-comment/create-and-start",
            "post_comment",
            {"target_comments_per_message": 1, "comment_count_jitter": 0, "topic_hint": "持续评论"},
            "reply_channel_message",
            lambda calls: (lambda *args, **kwargs: calls.append(args[2]) or SendResult(True, remote_message_id=f"comment-{len(calls)}")),
        ),
    ],
)
def test_task_center_channel_view_and_comment_default_dynamic_new_keep_collecting(monkeypatch, endpoint, action_type, payload_extra, gateway_attr, result_factory):
    fetched_ids = [5101]
    calls: list[int] = []

    def fake_fetch_channel_messages(*args, **kwargs):
        return [
            ChannelMessageSnapshot(
                message_id=fetched_ids[-1],
                content_preview=f"默认持续监听消息 {fetched_ids[-1]}",
                message_url=f"https://t.me/pytest_default_dynamic/{fetched_ids[-1]}",
                published_at=datetime.now(UTC).replace(tzinfo=None),
            )
        ]

    monkeypatch.setattr("app.services.task_center.executors.common.gateway.fetch_channel_messages", fake_fetch_channel_messages)
    monkeypatch.setattr(f"app.services.task_center.dispatcher.gateway.{gateway_attr}", result_factory(calls))
    from app.services.task_center.service import drain_task_center

    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-default-dynamic-{uuid4().hex[:8]}",
                "title": "pytest 默认持续频道",
                "username": "pytest_default_dynamic",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        mark_test_channel_comment_ready(channel_target["id"], [account["id"]])
        created = client.post(
            endpoint,
            headers=headers,
            json={
                "name": f"pytest default dynamic {action_type}",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 3600, "interval_seconds_max": 3600, "jitter_percent": 0},
                "target_channel_id": channel_target["id"],
                "message_count": 1,
                **payload_extra,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]

        assert drain_task_center(SessionLocal, 10) >= 1
        if calls == []:
            assert drain_task_center(SessionLocal, 10) >= 1
        assert calls == [5101]

        with SessionLocal() as session:
            task = session.get(Task, task_id)
            assert task.type_config["message_scope"] == "dynamic_new"
            next_run_at = task.next_run_at.replace(tzinfo=None) if task.next_run_at.tzinfo else task.next_run_at
            assert next_run_at <= datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=45)
            assert session.query(Action).filter(Action.task_id == task_id, Action.action_type == action_type).count() == 1
            task.next_run_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
            session.commit()

        fetched_ids.append(5102)
        reset_listener_runtime_cache()
        assert drain_task_center(SessionLocal, 10) >= 1
        if calls == [5101]:
            assert drain_task_center(SessionLocal, 10) >= 1
        assert calls == [5101, 5102]

        detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        assert detail["task"]["status"] == "running"
        assert detail["task"]["last_error"] == ""
        actions = task_detail_actions(client, headers, task_id)
        assert sorted(action["payload"]["message_id"] for action in actions if action["action_type"] == action_type) == [5101, 5102]


def test_task_center_reset_channel_like_rebuilds_from_latest_messages(monkeypatch):
    fetched_ids = [4101]
    reactions: list[int] = []

    def fake_fetch_channel_messages(*args, **kwargs):
        return [
            ChannelMessageSnapshot(
                message_id=fetched_ids[-1],
                content_preview=f"自动采集频道消息 {fetched_ids[-1]}",
                message_url=f"https://t.me/pytest_reset_channel/{fetched_ids[-1]}",
                published_at=datetime.now(UTC).replace(tzinfo=None),
            )
        ]

    monkeypatch.setattr("app.services.task_center.executors.common.gateway.fetch_channel_messages", fake_fetch_channel_messages)
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_channel_reaction",
        lambda *args, **kwargs: reactions.append(args[2]) or OperationResult(True, detail="liked"),
    )
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-reset-channel-{uuid4().hex[:8]}",
                "title": "pytest 重置采集频道",
                "username": "pytest_reset_channel",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        created = client.post(
            "/api/tasks/channel-like",
            headers=headers,
            json={
                "name": "pytest reset like",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                "target_channel_id": channel_target["id"],
                "message_scope": "latest_n",
                "message_count": 1,
                "target_likes_per_message": 1,
                "like_count_jitter": 0,
                "allowed_reactions": ["👍"],
                "max_likes_per_account_per_hour": 999,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        client.post(f"/api/tasks/{task_id}/start", headers=headers)
        from app.services.task_center.service import drain_task_center

        drain_task_center(SessionLocal, 10)
        assert reactions == [4101]

        with SessionLocal() as session:
            old_action_count = session.query(Action).filter(Action.task_id == task_id).count()
            assert old_action_count == 1
            action = session.query(Action).filter(Action.task_id == task_id).one()
            action.status = "failed"
            action.result = {"success": False, "error_message": "old failure"}
            session.add(
                ReviewQueue(
                    tenant_id=1,
                    task_id=task_id,
                    action_id=action.id,
                    content_preview="old review",
                    status="pending",
                    expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1),
                )
            )
            session.commit()

        fetched_ids.append(4202)
        reset = client.post(f"/api/tasks/{task_id}/reset", headers=headers, json={"reason": "测试重置任务"})
        assert reset.status_code == 200, reset.text
        detail_after_reset = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        actions_after_reset = task_detail_actions(client, headers, task_id)
        assert len(actions_after_reset) == 1
        assert actions_after_reset[0]["status"] == "failed"
        assert "reviews" not in detail_after_reset
        assert detail_after_reset["task"]["status"] == "running"
        assert detail_after_reset["task"]["stats"]["total_actions"] == 1

        drain_task_center(SessionLocal, 10)
        detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        assert reactions[0] == 4101
        assert 4202 in reactions
        actions = task_detail_actions(client, headers, task_id)
        assert len(actions) == 2
        assert any(action["payload"]["message_id"] == 4202 for action in actions)
        assert detail["task"]["stats"]["success_count"] == 1


def test_task_center_reset_channel_view_rebuilds_from_latest_messages(monkeypatch):
    fetched_ids = [4301]
    views: list[int] = []

    def fake_fetch_channel_messages(*args, **kwargs):
        return [
            ChannelMessageSnapshot(
                message_id=fetched_ids[-1],
                content_preview=f"自动采集浏览消息 {fetched_ids[-1]}",
                message_url=f"https://t.me/pytest_reset_view/{fetched_ids[-1]}",
                published_at=datetime.now(UTC).replace(tzinfo=None),
            )
        ]

    monkeypatch.setattr("app.services.task_center.executors.common.gateway.fetch_channel_messages", fake_fetch_channel_messages)
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.view_channel_message",
        lambda *args, **kwargs: views.append(args[2]) or OperationResult(True, detail="viewed"),
    )
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-reset-view-{uuid4().hex[:8]}",
                "title": "pytest 重置浏览频道",
                "username": "pytest_reset_view",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        created = client.post(
            "/api/tasks/channel-view",
            headers=headers,
            json={
                "name": "pytest reset view",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                "target_channel_id": channel_target["id"],
                "message_scope": "latest_n",
                "message_count": 1,
                "target_views_per_message": 1,
                "view_count_jitter": 0,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        client.post(f"/api/tasks/{task_id}/start", headers=headers)
        from app.services.task_center.service import drain_task_center

        drain_task_center(SessionLocal, 10)
        assert views == [4301]

        fetched_ids.append(4302)
        reset = client.post(f"/api/tasks/{task_id}/reset", headers=headers, json={"reason": "测试重置任务"})
        assert reset.status_code == 200, reset.text
        detail_after_reset = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        actions_after_reset = task_detail_actions(client, headers, task_id)
        assert len(actions_after_reset) == 1
        assert actions_after_reset[0]["status"] == "success"
        assert "reviews" not in detail_after_reset

        drain_task_center(SessionLocal, 10)
        detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        assert views[0] == 4301
        assert 4302 in views
        actions = task_detail_actions(client, headers, task_id)
        assert len(actions) == 2
        assert all(action["action_type"] == "view_message" for action in actions)
        assert any(action["payload"]["message_id"] == 4302 for action in actions)
        assert detail["task"]["stats"]["success_count"] == 2


def test_task_center_reset_channel_comment_rebuilds_auto_plan(monkeypatch):
    fetched_ids = [4401]
    comments: list[tuple[int, str]] = []

    def fake_fetch_channel_messages(*args, **kwargs):
        return [
            ChannelMessageSnapshot(
                message_id=fetched_ids[-1],
                content_preview=f"自动采集评论消息 {fetched_ids[-1]}",
                message_url=f"https://t.me/pytest_reset_comment/{fetched_ids[-1]}",
                published_at=datetime.now(UTC).replace(tzinfo=None),
            )
        ]

    monkeypatch.setattr("app.services.task_center.executors.common.gateway.fetch_channel_messages", fake_fetch_channel_messages)
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.reply_channel_message",
        lambda *args, **kwargs: comments.append((args[2], args[3])) or SendResult(True, remote_message_id=f"comment-{len(comments)}"),
    )
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-reset-comment-{uuid4().hex[:8]}",
                "title": "pytest 重置评论频道",
                "username": "pytest_reset_comment",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        mark_test_channel_comment_ready(channel_target["id"], [account["id"]])
        created = client.post(
            "/api/tasks/channel-comment",
            headers=headers,
            json={
                "name": "pytest reset comment",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                "target_channel_id": channel_target["id"],
                "message_scope": "latest_n",
                "message_count": 1,
                "target_comments_per_message": 1,
                "comment_count_jitter": 0,
                "topic_hint": "reset comment",
                "require_review": True,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        client.post(f"/api/tasks/{task_id}/start", headers=headers)
        from app.services.task_center.service import drain_task_center

        drain_task_center(SessionLocal, 10)
        old_detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        old_actions = task_detail_actions(client, headers, task_id)
        assert len(old_actions) == 1
        assert "reviews" not in old_detail
        assert old_actions[0]["payload"]["message_id"] == 4401
        assert comments == [(4401, old_actions[0]["payload"]["comment_text"])]

        fetched_ids.append(4402)
        reset = client.post(f"/api/tasks/{task_id}/reset", headers=headers, json={"reason": "测试重置任务"})
        assert reset.status_code == 200, reset.text
        detail_after_reset = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        actions_after_reset = task_detail_actions(client, headers, task_id)
        assert len(actions_after_reset) == 1
        assert actions_after_reset[0]["status"] == "success"
        assert "reviews" not in detail_after_reset

        drain_task_center(SessionLocal, 10)
        detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        actions = task_detail_actions(client, headers, task_id)
        assert len(actions) == 2
        assert any(action["action_type"] == "post_comment" and action["payload"]["message_id"] == 4402 for action in actions)
        assert "reviews" not in detail

        drain_task_center(SessionLocal, 10)
        final_detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        new_action = next(action for action in actions if action["payload"]["message_id"] == 4402)
        assert comments[-1] == (4402, new_action["payload"]["comment_text"])
        assert final_detail["task"]["stats"]["success_count"] == 2


def test_task_center_reset_group_ai_chat_respects_idle_window(monkeypatch):
    sends: list[str] = []
    generated = {"count": 0}

    def fake_generate_drafts(_credentials, _prompt, **_kwargs):
        generated["count"] += 1
        contents = [
            "接刚才真人那句，郑州楼凤最近哪家反馈稳点？",
            "上面说的郑州楼凤新上下文我看到了，最近上榜那几个咋样？",
            "主任最近有试新妹子的吗，群里谁知道服务细节？",
        ]
        count = int(_kwargs.get("count") or 1)
        candidates = []
        for index in range(count):
            token = _workflow_ai_token(index)
            content = contents[(generated["count"] + index - 1) % len(contents)]
            candidates.append(AiDraftCandidate(persona="自然群友", content=f"pytest-reset-{token} {content}"))
        return AiGenerationResult(candidates=candidates, usage=AiUsage(total_tokens=10))

    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_message",
        lambda *args, **kwargs: sends.append(args[2]) or SendResult(True, remote_message_id=f"reset-ai-{len(sends)}"),
    )
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        group = make_isolated_ai_group(account["id"], "pytest AI 重置")
        enable_mock_ai_provider(client, headers, "pytest AI 活跃重置")
        created = client.post(
            "/api/tasks/group-ai-chat",
            headers=headers,
            json={
                "name": "pytest reset AI 活跃",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": workflow_ai_active_pacing(),
                "target_group_id": group["id"],
                "topic_directions": [{"title": "reset ai", "weight": 1}],
                "participation_rate": 1,
                "participation_jitter": 0,
                "messages_per_round_mode": "manual",
                "messages_per_round": 1,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        started = client.post(f"/api/tasks/{task_id}/start", headers=headers)
        assert started.status_code == 200, started.text
        from app.services.task_center.service import drain_task_center

        drain_task_center(SessionLocal, 10)
        make_task_send_actions_due(task_id)
        drain_task_center(SessionLocal, 10)
        initial_detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        initial_actions = task_detail_actions(client, headers, task_id)
        initial_action_count = len(initial_actions)
        assert initial_action_count >= 1, {
            "task": compact_task_debug(initial_detail["task"]),
            "actions": compact_action_debug(initial_actions),
            "generated_count": generated["count"],
        }
        with SessionLocal() as session:
            past_time = _now() - timedelta(minutes=10)
            for action in session.scalars(select(Action).where(Action.task_id == task_id)):
                action.scheduled_at = past_time
                if action.status == "success":
                    action.executed_at = past_time
            session.commit()
        known_action_ids = {str(action.get("id") or "") for action in initial_actions}
        sends_before_reset = len(sends)
        reset = client.post(f"/api/tasks/{task_id}/reset", headers=headers, json={"reason": "测试重置任务"})
        assert reset.status_code == 200, reset.text
        detail_after_reset = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        assert detail_after_reset["task"]["status"] == "running", {
            "initial": compact_task_debug(initial_detail["task"]),
            "after_reset": compact_task_debug(detail_after_reset["task"]),
        }

        with SessionLocal() as session:
            session.add(
                GroupContextMessage(
                    tenant_id=1,
                    group_id=group["id"],
                    listener_account_id=account["id"],
                    sender_name="真人用户",
                    content=f"郑州楼凤 reset ai 新上下文 {_workflow_ai_token(9)} 最近哪家反馈稳点",
                    message_type="text",
                    remote_message_id=f"reset-ai-context-{uuid4().hex[:8]}",
                    sent_at=_now(),
                )
            )
            task = session.get(Task, task_id)
            assert task is not None
            task.next_run_at = _now()
            session.commit()

        detail, reset_cycle_actions = wait_for_new_success_actions(client, headers, task_id, known_action_ids)
        if not reset_cycle_actions:
            assert detail["task"]["status"] == "running"
            assert detail["task"]["next_run_at"]
            assert len(sends) == sends_before_reset
            return
        assert reset_cycle_actions, {
            "task": compact_task_debug(detail["task"]),
            "actions": compact_action_debug(task_detail_actions(client, headers, task_id)),
        }
        assert any(action["status"] == "success" for action in reset_cycle_actions), compact_action_debug(
            reset_cycle_actions
        )
        assert len(sends) > sends_before_reset


def test_task_center_reset_group_relay_clears_source_fingerprints():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        created = client.post(
            "/api/tasks/group-relay",
            headers=headers,
            json={
                "name": "pytest reset relay",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                "source_groups": [{"group_id": group["id"], "group_name": group["title"], "is_active": True}],
                "target_group_id": group["id"],
                "content_mode": "raw",
                "filters": {"keyword_whitelist": ["转发监听消息"]},
                "dedup_window_minutes": 60,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        with SessionLocal() as session:
            task = session.get(Task, task_id)
            session.add(MessageFingerprint(tenant_id=1, source_group_id=f"{task_id}:relay:{group['id']}", fingerprint="pytest-reset", original_text="old"))
            session.add(Action(tenant_id=1, task_id=task_id, task_type=task.type, action_type="send_message", account_id=account["id"], scheduled_at=datetime.now(UTC).replace(tzinfo=None), status="success", payload={"message_text": "old"}, result={"success": True}))
            session.commit()

        reset = client.post(f"/api/tasks/{task_id}/reset", headers=headers, json={"reason": "测试重置任务"})
        assert reset.status_code == 200, reset.text
        with SessionLocal() as session:
            assert session.query(Action).filter(Action.task_id == task_id).count() == 1
            assert session.query(MessageFingerprint).filter(MessageFingerprint.source_group_id == f"{task_id}:relay:{group['id']}").count() == 1


def test_task_center_channel_task_reports_no_collect_account():
    with TestClient(app) as client:
        headers = auth_headers(client)
        ensure_test_workspace(client, headers)
        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-no-account-channel-{uuid4().hex[:8]}",
                "title": "pytest 无采集账号频道",
                "username": "pytest_no_collect_account",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        created = client.post(
            "/api/tasks/channel-like",
            headers=headers,
            json={
                "name": "pytest no collect account",
                "account_config": {"selection_mode": "manual", "account_ids": [999999], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                "target_channel_id": channel_target["id"],
                "message_scope": "latest_n",
                "message_count": 1,
                "target_likes_per_message": 1,
                "like_count_jitter": 0,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        client.post(f"/api/tasks/{task_id}/start", headers=headers)
        from app.services.task_center.service import drain_task_center

        drain_task_center(SessionLocal, 10)

        detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()["task"]
        assert detail["status"] == "running"
        assert detail["last_error"] == "没有可用于采集频道消息的账号"


def _clear_relay_source_context(session, group_id: int) -> None:
    session.query(SourceMediaAsset).filter(SourceMediaAsset.source_group_id == group_id).delete(synchronize_session=False)
    session.query(GroupContextMessage).filter(GroupContextMessage.group_id == group_id).delete(synchronize_session=False)


def test_task_center_group_relay_auto_executes_and_dedupes(monkeypatch):
    sends: list[str] = []
    monkeypatch.setattr(
        "app.services.task_center.executors.group_relay.should_collect_listener",
        lambda *args, **kwargs: False,
    )

    def fake_send_message(account_id, group_id, content, outbound_segments, account_session, peer_id=None, developer_credentials=None):
        sends.append(content)
        return SendResult(True, remote_message_id=f"relay-{len(sends)}")

    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.send_message", fake_send_message)
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            _clear_relay_source_context(session, group["id"])
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            session.add(
                GroupContextMessage(
                    tenant_id=1,
                    group_id=group["id"],
                    listener_account_id=account["id"],
                    sender_peer_id="pytest-user",
                    sender_name="pytest",
                    content="新品上线，适合转发到目标群",
                    remote_message_id=f"src-{uuid4().hex[:8]}",
                    sent_at=datetime.now(UTC),
                )
            )
            session.commit()
        created = client.post(
            "/api/tasks/group-relay",
            headers=headers,
            json={
                "name": "pytest relay review",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                "failure_policy": {"max_retries": 1, "retry_delay_seconds": 0, "retry_backoff": "none"},
                "source_groups": [{"group_id": group["id"], "group_name": group["title"], "is_active": True}],
                "target_group_id": group["id"],
                "content_mode": "raw",
                "filters": {"keyword_whitelist": ["新品"]},
                "dedup_window_minutes": 60,
                "require_review": True,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        client.post(f"/api/tasks/{task_id}/start", headers=headers)

        from app.services.task_center.service import drain_task_center

        assert drain_task_center(SessionLocal, 10) >= 1
        assert drain_task_center(SessionLocal, 10) >= 0
        assert drain_task_center(SessionLocal, 10) == 0
        assert sends == ["新品上线，适合转发到目标群"]
        reviews = [item for item in client.get("/api/review-queue", headers=headers).json() if item["task_id"] == task_id]
        assert reviews == []
        detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        assert detail["task"]["stats"]["total_actions"] == 1
        assert detail["task"]["stats"]["success_count"] == 1


def test_group_relay_waits_for_source_media_cache_and_preserves_album_order(monkeypatch):
    send_calls: list[list[tuple[str, str | None, str]]] = []
    monkeypatch.setattr(
        "app.services.task_center.executors.group_relay.should_collect_listener",
        lambda *args, **kwargs: False,
    )

    def fake_send_message(account_id, group_id, content, outbound_segments, account_session, peer_id=None, developer_credentials=None):
        send_calls.append([(segment.segment_type, segment.source, segment.caption) for segment in outbound_segments])
        return SendResult(True, remote_message_id=f"relay-media-{len(send_calls)}")

    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.send_message", fake_send_message)
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        remote_id = f"album-{uuid4().hex[:8]}"
        with SessionLocal() as session:
            _clear_relay_source_context(session, group["id"])
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            for link in session.query(TgGroupAccount).filter_by(group_id=group["id"]):
                link.can_send = True
                link.is_listener = link.account_id == account["id"]
            session.add(
                GroupContextMessage(
                    tenant_id=1,
                    group_id=group["id"],
                    listener_account_id=account["id"],
                    sender_peer_id="pytest-media-user",
                    sender_name="pytest",
                    content="相册源消息",
                    message_type="media",
                    remote_message_id=remote_id,
                    sent_at=datetime.now(UTC),
                )
            )
            assets = [
                SourceMediaAsset(
                    tenant_id=1,
                    source_group_id=group["id"],
                    listener_account_id=account["id"],
                    source_peer_id=group["tg_peer_id"],
                    source_message_id=remote_id,
                    source_media_group_id="media-group-1",
                    media_group_index=index,
                    media_group_total=3,
                    media_type="photo",
                    caption=f"图{index}",
                    cache_status="pending_cache",
                )
                for index in [1, 2, 3]
            ]
            session.add_all(assets)
            session.commit()

        created = client.post(
            "/api/tasks/group-relay",
            headers=headers,
            json={
                "name": "pytest relay media album",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                "failure_policy": {"max_retries": 1, "retry_delay_seconds": 0, "retry_backoff": "none"},
                "source_groups": [{"group_id": group["id"], "group_name": group["title"], "is_active": True}],
                "target_group_id": group["id"],
                "content_mode": "raw",
                "filters": {"allowed_media_types": ["media", "photo"]},
                "preserve_media": True,
                "dedup_window_minutes": 60,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        client.post(f"/api/tasks/{task_id}/start", headers=headers)

        from app.services.task_center.service import drain_task_center
        from app.services.source_media import WAITING_MATERIAL_CACHE, source_media_cached_event, wake_waiting_actions_for_source_media

        assert drain_task_center(SessionLocal, 10) >= 1
        with SessionLocal() as session:
            action = session.query(Action).filter_by(task_id=task_id, action_type="send_message").first()
            assert action is not None
            assert action.status == WAITING_MATERIAL_CACHE
            assert action.result["error_code"] == "waiting_material_cache"
            asset_rows = list(
                session.query(SourceMediaAsset)
                .filter_by(source_group_id=group["id"], source_message_id=remote_id)
                .order_by(SourceMediaAsset.media_group_index.asc())
            )
            assert [asset.media_group_index for asset in asset_rows] == [1, 2, 3]
            source_media_cached_event(session, source_media_asset_id=asset_rows[0].id, cache_peer_id="cache-peer", cache_message_id="201", cache_version=1)
            assert action.status == WAITING_MATERIAL_CACHE
            assert action.payload.get("media_segments") in (None, [])
            assert wake_waiting_actions_for_source_media(session, 1) == 0
            assert action.status == WAITING_MATERIAL_CACHE
            source_media_cached_event(session, source_media_asset_id=asset_rows[2].id, cache_peer_id="cache-peer", cache_message_id="203", cache_version=1)
            assert wake_waiting_actions_for_source_media(session, 1) == 0
            assert action.status == WAITING_MATERIAL_CACHE
            asset_rows[1].cache_status = "cache_failed"
            asset_rows[1].failure_reason = "download_failed"
            assert wake_waiting_actions_for_source_media(session, 1) == 1
            session.commit()

        assert drain_task_center(SessionLocal, 10) >= 1
        assert send_calls == [[
            ("文本", None, ""),
            ("图片", "tg-cache://cache-peer/201", "图1"),
            ("图片", "tg-cache://cache-peer/203", "图3"),
        ]]
        with SessionLocal() as session:
            action = session.query(Action).filter_by(task_id=task_id, action_type="send_message").first()
            assert action.status == "success"
            results = action.payload["album_segment_results"]
            assert [item["media_group_index"] for item in results] == [1, 2, 3]
            assert results[1]["status"] == "album_segment_failed"


def test_task_center_group_relay_continues_for_new_source_messages(monkeypatch):
    sends: list[str] = []
    monkeypatch.setattr(
        "app.services.task_center.executors.group_relay.should_collect_listener",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_message",
        lambda *args, **kwargs: sends.append(args[2]) or SendResult(True, remote_message_id=f"relay-continuous-{len(sends)}"),
    )
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            _clear_relay_source_context(session, group["id"])
            session.add(
                GroupContextMessage(
                    tenant_id=1,
                    group_id=group["id"],
                    listener_account_id=account["id"],
                    sender_peer_id="pytest-user",
                    sender_name="pytest",
                    content="第一条转发监听消息",
                    remote_message_id=f"relay-src-{uuid4().hex[:8]}",
                    sent_at=datetime.now(UTC),
                )
            )
            session.commit()
        created = client.post(
            "/api/tasks/group-relay/create-and-start",
            headers=headers,
            json={
                "name": "pytest relay 持续监听",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                "source_groups": [{"group_id": group["id"], "group_name": group["title"], "is_active": True}],
                "target_group_id": group["id"],
                "content_mode": "raw",
                "filters": {"keyword_whitelist": ["转发监听消息"]},
                "dedup_window_minutes": 60,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]

        from app.services.task_center.service import drain_task_center

        drain_task_center(SessionLocal, 1000)
        drain_task_center(SessionLocal, 1000)
        assert sends == ["第一条转发监听消息"]

        with SessionLocal() as session:
            session.add(
                GroupContextMessage(
                    tenant_id=1,
                    group_id=group["id"],
                    listener_account_id=account["id"],
                    sender_peer_id="pytest-user",
                    sender_name="pytest",
                    content="第二条转发监听消息",
                    remote_message_id=f"relay-src-{uuid4().hex[:8]}",
                    sent_at=datetime.now(UTC),
                )
            )
            task = session.get(Task, task_id)
            assert task is not None
            task.next_run_at = _now()
            session.commit()
        reset_listener_runtime_cache()
        drain_task_center(SessionLocal, 1000)
        drain_task_center(SessionLocal, 1000)
        detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        assert sends == ["第一条转发监听消息", "第二条转发监听消息"]
        assert detail["task"]["status"] == "running"
        assert detail["task"]["stats"]["success_count"] == 2


def test_task_center_pause_holds_due_actions(monkeypatch):
    sends: list[str] = []
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_message",
        lambda *args, **kwargs: sends.append(args[2]) or SendResult(True, remote_message_id="paused-send"),
    )
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        task = client.post(
            "/api/tasks/group-ai-chat",
            headers=headers,
            json={
                "name": "pytest paused action",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                "target_group_id": group["id"],
            },
        ).json()
        client.post(f"/api/tasks/{task['id']}/start", headers=headers)
        client.post(f"/api/tasks/{task['id']}/pause", headers=headers)
        with SessionLocal() as session:
            action = Action(
                tenant_id=1,
                task_id=task["id"],
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=account["id"],
                scheduled_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1),
                status="pending",
                payload={"group_id": group["id"], "chat_id": group["tg_peer_id"], "message_text": "暂停后不应发送", "review_approved": True},
                result={},
            )
            session.add(action)
            session.commit()
            action_id = action.id

        client.post("/api/worker/drain-once", headers=headers, json={"reason": "测试手动 drain"})
        with SessionLocal() as session:
            assert session.get(Action, action_id).status == "pending"
        assert "暂停后不应发送" not in sends


def test_task_center_pending_reviews_do_not_starve_other_due_actions(monkeypatch):
    sends: list[str] = []
    from app.services.task_center import dispatcher

    monkeypatch.setattr(dispatcher, "get_settings", lambda: type("Settings", (), {"enable_legacy_review_dispatch_gate": True})())
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_message",
        lambda *args, **kwargs: sends.append(args[2]) or SendResult(True, remote_message_id="normal-send"),
    )
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        now = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        with SessionLocal() as session:
            db_group = session.get(TgGroup, group["id"])
            db_group.can_send = True
            db_group.daily_limit = 10000
            db_group.group_cooldown_seconds = 0
            db_group.banned_words = ""
            link = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=account["id"]).first()
            if link:
                link.can_send = True
            blocked_task = Task(tenant_id=1, name="pytest pending review", type="group_relay", status="running", next_run_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1), account_config={}, pacing_config={}, failure_policy={}, type_config={}, stats={})
            normal_task = Task(tenant_id=1, name="pytest normal action", type="group_ai_chat", status="running", next_run_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1), account_config={}, pacing_config={}, failure_policy={}, type_config={}, stats={})
            session.add_all([blocked_task, normal_task])
            session.flush()
            blocked_action = Action(tenant_id=1, task_id=blocked_task.id, task_type=blocked_task.type, action_type="send_message", account_id=account["id"], scheduled_at=now, status="pending", payload={"group_id": group["id"], "message_text": "待审核内容"}, result={})
            normal_action = Action(tenant_id=1, task_id=normal_task.id, task_type=normal_task.type, action_type="send_message", account_id=account["id"], scheduled_at=now, status="pending", payload={"group_id": group["id"], "message_text": "普通内容", "review_approved": True}, result={})
            session.add_all([blocked_action, normal_action])
            session.flush()
            session.add(ReviewQueue(tenant_id=1, task_id=blocked_task.id, action_id=blocked_action.id, content_preview="待审核内容", status="pending", expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)))
            session.commit()
            blocked_action_id = blocked_action.id
            normal_action_id = normal_action.id

        client.post("/api/worker/drain-once", headers=headers, json={"reason": "测试手动 drain"})
        with SessionLocal() as session:
            assert session.get(Action, blocked_action_id).status == "pending"
            assert session.get(Action, normal_action_id).status == "success"
        assert sends.count("普通内容") == 1
        assert "待审核内容" not in sends


def test_task_center_review_terminal_state_cannot_be_approved(monkeypatch):
    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.send_message", lambda *args, **kwargs: SendResult(True, remote_message_id="should-not-send"))
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            task = Task(tenant_id=1, name="pytest terminal review", type="group_relay", status="running", next_run_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1), account_config={}, pacing_config={}, failure_policy={}, type_config={}, stats={})
            session.add(task)
            session.flush()
            action = Action(tenant_id=1, task_id=task.id, task_type=task.type, action_type="send_message", account_id=account["id"], scheduled_at=datetime.now(UTC).replace(tzinfo=None), status="pending", payload={"group_id": group["id"], "message_text": "拒绝后不应复活"}, result={})
            session.add(action)
            session.flush()
            review = ReviewQueue(tenant_id=1, task_id=task.id, action_id=action.id, content_preview="拒绝后不应复活", status="pending", expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1))
            session.add(review)
            session.commit()
            review_id = review.id
            action_id = action.id

        rejected = client.post(f"/api/review/{review_id}/reject", headers=headers, json={"reason": "不通过"})
        assert rejected.status_code == 200, rejected.text
        approved = client.post(f"/api/review/{review_id}/approve", headers=headers, json={"edited_content": "复活内容"})
        assert approved.status_code == 409
        with SessionLocal() as session:
            assert session.get(Action, action_id).status == "skipped"


def test_task_center_channel_specific_scope_requires_message_ids():
    with TestClient(app) as client:
        headers = auth_headers(client)
        response = client.post(
            "/api/tasks/channel-view",
            headers=headers,
            json={
                "name": "pytest bad specific scope",
                "account_config": {"selection_mode": "all"},
                "pacing_config": {"mode": "template", "template": "moderate_6h"},
                "target_channel_id": 1,
                "message_scope": "specific",
                "message_ids": [],
                "target_views_per_message": 1,
            },
        )
        assert response.status_code in {400, 422}
        assert "message_ids" in response.text


def test_task_center_create_and_start_rolls_back_when_start_fails(monkeypatch):
    import app.services.task_center.service as task_center_service

    def fail_start(*args, **kwargs):
        raise ValueError("启动失败")

    monkeypatch.setattr(task_center_service, "_mark_task_started", fail_start)
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-atomic-channel-{uuid4().hex[:8]}",
                "title": "pytest 原子创建频道",
                "username": "pytest_atomic_channel",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        response = client.post(
            "/api/tasks/channel-like/create-and-start",
            headers=headers,
            json={
                "name": "pytest atomic create rollback",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                "target_channel_id": channel_target["id"],
                "message_scope": "latest_n",
                "message_count": 1,
                "target_likes_per_message": 1,
                "like_count_jitter": 0,
            },
        )
        assert response.status_code == 400
        with SessionLocal() as session:
            assert session.query(Task).filter(Task.name == "pytest atomic create rollback").count() == 0


def test_task_center_type_specific_create_rejects_unrelated_fields():
    with TestClient(app) as client:
        headers = auth_headers(client)
        response = client.post(
            "/api/tasks/channel-like",
            headers=headers,
            json={
                "name": "pytest mixed fields rejected",
                "account_config": {"selection_mode": "all"},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0},
                "target_channel_id": 1,
                "message_scope": "latest_n",
                "message_count": 1,
                "target_likes_per_message": 1,
                "comment_style": "mixed",
                "target_comments_per_message": 3,
            },
        )
        assert response.status_code == 422
        assert "target_comments_per_message" in response.text


def test_task_center_common_patch_rejects_type_config_and_typed_patch_checks_task_type():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        task = client.post(
            "/api/tasks/group-ai-chat",
            headers=headers,
            json={
                "name": "pytest typed patch boundary",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]]},
                "target_group_id": group["id"],
            },
        )
        assert task.status_code == 200, task.text
        task_id = task.json()["id"]

        generic = client.patch(f"/api/tasks/{task_id}", headers=headers, json={"type_config": {"target_likes_per_message": 1}})
        assert generic.status_code == 422
        assert "type_config" in generic.text

        mismatch = client.patch(
            f"/api/tasks/{task_id}/channel-like",
            headers=headers,
            json={"target_channel_id": 1, "message_scope": "latest_n", "message_count": 1, "target_likes_per_message": 1},
        )
        assert mismatch.status_code == 400
        assert "任务类型不匹配" in mismatch.text


def test_task_center_dispatcher_rejects_mixed_action_payload(monkeypatch):
    def should_not_like(*args, **kwargs):
        raise AssertionError("invalid like payload must not reach Telegram gateway")

    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.send_channel_reaction", should_not_like)
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        now = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            task = Task(tenant_id=1, name="pytest mixed payload", type="channel_like", status="running", next_run_at=now + timedelta(days=1), account_config={}, pacing_config={}, failure_policy={}, type_config={}, stats={})
            session.add(task)
            session.flush()
            action = Action(
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="like_message",
                account_id=account["id"],
                scheduled_at=now,
                status="pending",
                payload={"channel_id": "pytest-channel", "message_id": 1, "reaction_emoji": "👍", "message_text": "不该出现在点赞 payload"},
                result={},
            )
            session.add(action)
            session.commit()
            action_id = action.id

        with SessionLocal() as session:
            from app.services.task_center.dispatcher import dispatch_action

            dispatch_action(session, session.get(Action, action_id))
            session.commit()
            row = session.get(Action, action_id)
            assert row.status == "failed"
            assert "message_text" in row.result["error_message"]


def test_task_center_send_message_payload_requires_destination(monkeypatch):
    def should_not_send(*args, **kwargs):
        raise AssertionError("payload without destination must not reach Telegram gateway")

    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.send_message", should_not_send)
    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.send_message_to_target", should_not_send)
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        now = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            task = Task(tenant_id=1, name="pytest send payload destination", type="group_ai_chat", status="running", next_run_at=now + timedelta(days=1), account_config={}, pacing_config={}, failure_policy={}, type_config={}, stats={})
            session.add(task)
            session.flush()
            action = Action(
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="send_message",
                account_id=account["id"],
                scheduled_at=now,
                status="pending",
                payload={"message_text": "没有目的地的消息"},
                result={},
            )
            session.add(action)
            session.commit()
            action_id = action.id

        client.post("/api/worker/drain-once", headers=headers, json={"reason": "测试手动 drain"})
        with SessionLocal() as session:
            row = session.get(Action, action_id)
            assert row.status == "failed"
            assert "group_id or chat_id" in row.result["error_message"]


def test_task_center_group_send_policy_ignores_legacy_message_cooldown():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        now = datetime.now(UTC).replace(tzinfo=None)
        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            db_group = session.get(TgGroup, group["id"])
            db_group.group_cooldown_seconds = 3600
            legacy = MessageTask(
                tenant_id=1,
                group_id=group["id"],
                account_id=account["id"],
                content="旧发送记录",
                status=TaskStatus.SENT.value,
                idempotency_key=f"pytest-legacy-sent-{uuid4().hex}",
                scheduled_at=now,
                sent_at=now,
            )
            task = Task(tenant_id=1, name="pytest legacy cooldown", type="group_ai_chat", status="running", next_run_at=now, account_config={}, pacing_config={}, failure_policy={}, type_config={}, stats={})
            session.add_all([legacy, task])
            session.flush()
            action = Action(
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="send_message",
                account_id=account["id"],
                scheduled_at=now - timedelta(seconds=1),
                status="pending",
                payload={"group_id": group["id"], "message_text": "应被旧任务冷却拦截", "review_approved": True},
                result={},
            )
            session.add(action)
            session.commit()
            action_id = action.id

        from app.services.task_center.policies import validate_group_send_policy

        with SessionLocal() as session:
            failure_type, failure_detail = validate_group_send_policy(
                session,
                tenant_id=1,
                group=session.get(TgGroup, group["id"]),
                content="旧任务发送记录不再触发隐藏群冷却",
                review_approved=True,
            )
            assert (failure_type, failure_detail) == (None, None)


def test_task_center_channel_failed_action_retries_before_task_failed(monkeypatch):
    calls: list[str] = []

    def flaky_like(*args, **kwargs):
        calls.append("like")
        if len(calls) == 1:
            return OperationResult(False, failure_type=FailureType.UNKNOWN.value, detail="temporary boom")
        return OperationResult(True, detail="liked")

    monkeypatch.setattr("app.services.task_center.dispatcher.gateway.send_channel_reaction", flaky_like)
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-retry-channel-{uuid4().hex[:8]}",
                "title": "pytest 重试频道",
                "username": "pytest_retry_channel",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        channel_message = client.post(
            "/api/channel-messages",
            headers=headers,
            json={
                "channel_target_id": channel_target["id"],
                "message_id": 3101,
                "message_url": "https://t.me/pytest_retry_channel/3101",
                "content_preview": "重试消息",
            },
        ).json()
        created = client.post(
            "/api/tasks/channel-like",
            headers=headers,
            json={
                "name": "pytest channel retry",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                "failure_policy": {"max_retries": 1, "retry_delay_seconds": 0, "retry_backoff": "none"},
                "target_channel_id": channel_target["id"],
                "message_scope": "specific",
                "message_ids": [channel_message["id"]],
                "target_likes_per_message": 1,
                "like_count_jitter": 0,
                "allowed_reactions": ["👍"],
                "max_likes_per_account_per_hour": 999,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        client.post(f"/api/tasks/{task_id}/start", headers=headers)

        from app.services.task_center.service import drain_task_center

        drain_task_center(SessionLocal, 1000)
        with SessionLocal() as session:
            task = session.get(Task, task_id)
            action = session.query(Action).filter(Action.task_id == task_id).one()
            assert task.status == "running"
            assert action.status == "failed"
            assert action.retry_count == 0
            task.next_run_at = _now()
            session.commit()

        drain_task_center(SessionLocal, 1000)
        with SessionLocal() as session:
            task = session.get(Task, task_id)
            action = session.query(Action).filter(Action.task_id == task_id).one()
            assert task.status == "running", {"task_status": task.status, "action_status": action.status, "retry_count": action.retry_count, "result": action.result}
            assert action.status == "success"
            assert action.retry_count == 1
            assert task.stats["success_count"] == 1
        assert calls == ["like", "like"]


def test_task_center_channel_like_respects_per_account_hour_limit(monkeypatch):
    monkeypatch.setattr(
        "app.services.task_center.dispatcher.gateway.send_channel_reaction",
        lambda *args, **kwargs: OperationResult(True, detail="liked"),
    )
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-rate-channel-{uuid4().hex[:8]}",
                "title": "pytest 频控频道",
                "username": "pytest_rate_channel",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        channel_messages = [
            client.post(
                "/api/channel-messages",
                headers=headers,
                json={
                    "channel_target_id": channel_target["id"],
                    "message_id": 3001 + index,
                    "message_url": f"https://t.me/pytest_rate_channel/{3001 + index}",
                    "content_preview": f"频控消息 {index}",
                },
            ).json()
            for index in range(3)
        ]
        created = client.post(
            "/api/tasks/channel-like",
            headers=headers,
            json={
                "name": "pytest like hour limit",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                "target_channel_id": channel_target["id"],
                "message_scope": "specific",
                "message_ids": [message["id"] for message in channel_messages],
                "target_likes_per_message": 1,
                "like_count_jitter": 0,
                "allowed_reactions": ["👍"],
                "max_likes_per_account_per_hour": 1,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        client.post(f"/api/tasks/{task_id}/start", headers=headers)
        client.post("/api/worker/drain-once", headers=headers, json={"reason": "测试手动 drain"})
        with SessionLocal() as session:
            rows = list(session.query(Action).filter(Action.task_id == task_id).order_by(Action.scheduled_at.asc(), Action.id.asc()))
        assert len(rows) == 3
        scheduled_hours = [row.scheduled_at.replace(minute=0, second=0, microsecond=0) for row in rows]
        assert scheduled_hours[1] > scheduled_hours[0]
        assert scheduled_hours[2] > scheduled_hours[1]


def test_task_center_max_duration_does_not_stop_continuous_tasks():
    from app.services.task_center.service import drain_task_center

    now = datetime.now(UTC).replace(tzinfo=None)
    with SessionLocal() as session:
        tasks = [
            Task(
                tenant_id=1,
                name=f"pytest max duration stays running {task_type}",
                type=task_type,
                status="running",
                max_duration_hours=1,
                next_run_at=now - timedelta(seconds=1),
                account_config={},
                pacing_config={"mode": "fixed", "interval_seconds_min": 3600, "interval_seconds_max": 3600},
                failure_policy={},
                type_config={},
                stats={"started_at": (now - timedelta(hours=3)).isoformat()},
            )
            for task_type in ["group_ai_chat", "group_relay", "channel_view", "channel_like", "channel_comment"]
        ]
        session.add_all(tasks)
        session.commit()
        task_ids = [task.id for task in tasks]

    drain_task_center(SessionLocal, 10)
    with SessionLocal() as session:
        assert {session.get(Task, task_id).status for task_id in task_ids} == {"running"}


def test_task_center_scheduled_end_stops_continuous_task():
    from app.services.task_center.service import drain_task_center

    now = datetime.now(UTC).replace(tzinfo=None)
    with SessionLocal() as session:
        task = Task(
            tenant_id=1,
            name="pytest scheduled end stops",
            type="channel_like",
            status="running",
            scheduled_end=now - timedelta(seconds=1),
            next_run_at=now - timedelta(seconds=1),
            account_config={},
            pacing_config={},
            failure_policy={},
            type_config={},
            stats={},
        )
        session.add(task)
        session.commit()
        task_id = task.id

    drain_task_center(SessionLocal, 10)
    with SessionLocal() as session:
        assert session.get(Task, task_id).status == "completed"


def test_task_center_no_available_accounts_warns_without_failing():
    from app.services.task_center.service import drain_task_center

    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-offline-channel-{uuid4().hex[:8]}",
                "title": "pytest 掉线频道",
                "username": "pytest_offline_channel",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        client.post(
            "/api/channel-messages",
            headers=headers,
            json={
                "channel_target_id": channel_target["id"],
                "message_id": 9101,
                "message_url": "https://t.me/pytest_offline_channel/9101",
                "content_preview": "账号掉线不应停任务",
            },
        )
        created = client.post(
            "/api/tasks/channel-like/create-and-start",
            headers=headers,
            json={
                "name": "pytest offline account keeps task running",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                "target_channel_id": channel_target["id"],
                "message_scope": "latest_n",
                "message_count": 1,
                "target_likes_per_message": 1,
                "like_count_jitter": 0,
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        with SessionLocal() as session:
            session.get(TgAccount, account["id"]).status = AccountStatus.NEED_RELOGIN.value
            session.commit()

        drain_task_center(SessionLocal, 10)
        with SessionLocal() as session:
            task = session.get(Task, task_id)
            assert task.status == "running"
            assert "没有可用账号" in task.last_error


def test_task_center_settings_updates_config_and_rebuilds_unfinished_plan():
    now = datetime.now(UTC).replace(tzinfo=None)
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _group = ensure_test_workspace(client, headers)
        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-settings-channel-{uuid4().hex[:8]}",
                "title": "pytest 设置频道",
                "username": "pytest_settings_channel",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        message = client.post(
            "/api/channel-messages",
            headers=headers,
            json={
                "channel_target_id": channel_target["id"],
                "message_id": 9201,
                "message_url": "https://t.me/pytest_settings_channel/9201",
                "content_preview": "设置变更消息",
            },
        ).json()
        with SessionLocal() as session:
            task = Task(
                tenant_id=1,
                name="pytest settings update",
                type="channel_like",
                status="running",
                next_run_at=now + timedelta(days=1),
                account_config={"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 60, "interval_seconds_max": 60},
                failure_policy={"max_retries": 1, "retry_delay_seconds": 0, "retry_backoff": "none"},
                type_config={
                    "target_channel_id": channel_target["id"],
                    "target_channel_name": channel_target["title"],
                    "message_scope": "specific",
                    "message_count": None,
                    "date_from": None,
                    "date_to": None,
                    "message_ids": [message["id"]],
                    "target_likes_per_message": 1,
                    "like_count_jitter": 0,
                    "reaction_type": "random",
                    "allowed_reactions": ["👍"],
                    "max_likes_per_account_per_hour": 999,
                },
                stats={},
            )
            session.add(task)
            session.flush()
            session.add_all(
                [
                    Action(tenant_id=1, task_id=task.id, task_type=task.type, action_type="like_message", account_id=account["id"], scheduled_at=now - timedelta(minutes=5), executed_at=now - timedelta(minutes=4), status="success", payload={"channel_message_id": message["id"], "message_id": 9201}, result={"success": True}),
                    Action(tenant_id=1, task_id=task.id, task_type=task.type, action_type="like_message", account_id=account["id"], scheduled_at=now + timedelta(minutes=5), status="pending", payload={"channel_message_id": message["id"], "message_id": 9201}, result={}),
                ]
            )
            session.commit()
            task_id = task.id

        updated = client.patch(
            f"/api/tasks/{task_id}/settings",
            headers=headers,
            json={
                "name": "pytest settings updated",
                "scheduled_end": (now + timedelta(days=1)).isoformat(),
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "pacing_config": {"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
                "failure_policy": {"max_retries": 2, "retry_delay_seconds": 0, "retry_backoff": "none"},
                "target_likes_per_message": 2,
                "like_count_jitter": 0,
                "reaction_type": "random",
                "allowed_reactions": ["👍", "❤️"],
                "max_likes_per_account_per_hour": 999,
            },
        )
        assert updated.status_code == 200, updated.text
        body = updated.json()
        assert body["status"] == "running"
        assert body["name"] == "pytest settings updated"
        assert body["type_config"]["target_likes_per_message"] == 2
        with SessionLocal() as session:
            rows = list(session.query(Action).filter(Action.task_id == task_id).order_by(Action.created_at.asc()))
            assert [row.status for row in rows] == ["success"]
            next_run_at = session.get(Task, task_id).next_run_at
            if next_run_at.tzinfo:
                next_run_at = next_run_at.replace(tzinfo=None)
            assert next_run_at <= _now()


def test_task_center_settings_accepts_target_scope_refresh():
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        created = client.post(
            "/api/tasks/group-ai-chat/create-and-start",
            headers=headers,
            json={
                "name": "pytest settings reject target",
                "account_config": {"selection_mode": "manual", "account_ids": [account["id"]], "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                "target_group_id": group["id"],
            },
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        response = client.patch(f"/api/tasks/{task_id}/settings", headers=headers, json={"target_group_id": group["id"]})
        assert response.status_code == 200, response.text
        assert response.json()["type_config"]["target_group_id"] == group["id"]
        assert "target_group_id" in response.text


def test_task_center_source_filter_override_endpoint_records_reason_and_identity():
    with TestClient(app) as client:
        headers = auth_headers(client)
        _account, group = ensure_test_workspace(client, headers)
        now = datetime.now(UTC).replace(tzinfo=None)
        with SessionLocal() as session:
            task = Task(
                tenant_id=1,
                name="pytest source override",
                type="group_relay",
                status="running",
                next_run_at=now + timedelta(days=1),
                account_config={"selection_mode": "all", "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 60, "interval_seconds_max": 60},
                failure_policy={"max_retries": 1, "retry_delay_seconds": 0, "retry_backoff": "none"},
                type_config={
                    "source_groups": [{"group_id": group["id"], "is_active": True}],
                    "target_group_id": group["id"],
                    "target_group_ids": [group["id"]],
                    "content_mode": "raw",
                    "excluded_sender_peer_ids": ["old-peer"],
                },
                stats={},
            )
            session.add(task)
            session.commit()
            task_id = task.id

        response = client.post(
            f"/api/tasks/{task_id}/source-filter-overrides",
            headers=headers,
            json={
                "sender_peer_id": "sender-42",
                "sender_username": "@pytest_sender",
                "sender_name": "来源用户",
                "source_action_id": "action-42",
                "source_action": "源群消息 action-42",
                "reason": "测试从任务详情加入不转发名单",
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["type_config"]["excluded_sender_peer_ids"] == ["old-peer", "sender-42"]
        assert body["type_config"]["excluded_sender_usernames"] == ["pytest_sender"]
        assert body["type_config"]["excluded_sender_names"] == ["来源用户"]
        with SessionLocal() as session:
            audit_log = session.query(AuditLog).filter_by(target_id=task_id, action="添加任务来源过滤覆盖").order_by(AuditLog.id.desc()).first()
            assert audit_log is not None
            assert audit_log.actor == "admin@demo.local"
            assert "sender-42" in audit_log.detail
            assert "pytest_sender" in audit_log.detail
            assert "action-42" in audit_log.detail
            assert "测试从任务详情加入不转发名单" in audit_log.detail


def test_task_center_source_filter_override_requires_source_action():
    with TestClient(app) as client:
        headers = auth_headers(client)
        _account, group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            task = Task(
                tenant_id=1,
                name="pytest source override missing action",
                type="group_relay",
                status="running",
                account_config={"selection_mode": "all", "max_concurrent": 1, "cooldown_per_account_minutes": 0},
                pacing_config={"mode": "fixed", "interval_seconds_min": 60, "interval_seconds_max": 60},
                failure_policy={"max_retries": 1, "retry_delay_seconds": 0, "retry_backoff": "none"},
                type_config={
                    "source_groups": [{"group_id": group["id"], "is_active": True}],
                    "target_group_id": group["id"],
                    "target_group_ids": [group["id"]],
                    "content_mode": "raw",
                },
                stats={},
            )
            session.add(task)
            session.commit()
            task_id = task.id

        response = client.post(
            f"/api/tasks/{task_id}/source-filter-overrides",
            headers=headers,
            json={
                "sender_peer_id": "sender-no-action",
                "reason": "缺失来源动作应拒绝",
            },
        )

        assert response.status_code == 422, response.text
        assert "source_action_id 或 source_action 至少提供一个" in response.text


def test_message_send_workbench_creates_private_group_channel_and_jitter_tasks(monkeypatch):
    send_calls: list[dict] = []

    def fake_send(account_id, group_id, content, outbound_segments, account_session, peer_id=None, developer_credentials=None):
        send_calls.append(
            {
                "account_id": account_id,
                "group_id": group_id,
                "content": content,
                "peer_id": peer_id,
                "segments": [(segment.segment_type, segment.source, segment.caption) for segment in outbound_segments],
            }
        )
        return SendResult(True, remote_message_id=f"message-send-{len(send_calls)}")

    monkeypatch.setattr("app.services.messages.gateway.send_message", fake_send)

    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        contacts = client.post(f"/api/tg-accounts/{account['id']}/contacts/sync", headers=headers).json()
        contact = next(item for item in contacts if item["username"] == "pytest_target")
        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            link = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=account["id"]).first()
            if not link:
                session.add(TgGroupAccount(tenant_id=1, group_id=group["id"], account_id=account["id"], can_send=True, permission_label="普通成员"))
                session.flush()
                link = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=account["id"]).first()
            link.can_send = True
            link.group.daily_limit = 999
            link.group.group_cooldown_seconds = 0
            link.group.account_cooldown_seconds = 0
            link.group.require_review = False
            setting = session.query(SchedulingSetting).filter_by(tenant_id=1).first()
            if not setting:
                setting = SchedulingSetting(tenant_id=1)
                session.add(setting)
            setting.jitter_min_seconds = 0
            setting.jitter_max_seconds = 0
            setting.batch_interval_seconds = 30
            session.commit()

        private_task = client.post(
            "/api/message-send-tasks",
            headers=headers,
            json={
                "account_id": account["id"],
                "target_type": "private",
                "target_peer_id": f"@{contact['username']}",
                "target_display": contact["display_name"],
                "content": "个人实时消息",
                "message_type": "文本",
                "dispatch_now": True,
            },
        )
        assert private_task.status_code == 200, private_task.text
        assert private_task.json()["status"] == TaskStatus.SENT.value
        assert private_task.json()["target_type"] == "private"
        assert send_calls[-1]["peer_id"] == "@pytest_target"

        group_task = client.post(
            "/api/message-send-tasks",
            headers=headers,
            json={
                "account_id": account["id"],
                "target_type": "group",
                "group_id": group["id"],
                "content": "群聊实时消息",
                "message_type": "文本",
                "dispatch_now": True,
            },
        )
        assert group_task.status_code == 200, group_task.text
        assert group_task.json()["status"] == TaskStatus.SENT.value
        assert group_task.json()["group_id"] == group["id"]
        assert send_calls[-1]["account_id"] == account["id"]

        channel_target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "tenant_id": 1,
                "target_type": "channel",
                "tg_peer_id": f"pytest-channel-{uuid4().hex[:8]}",
                "title": "消息发送频道",
                "username": "message_send_channel",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        with SessionLocal() as session:
            channel_group = TgGroup(
                tenant_id=1,
                tg_peer_id=channel_target["tg_peer_id"],
                title=channel_target["title"],
                group_type="channel",
                member_count=10,
                auth_status="已授权运营",
                can_send=True,
            )
            session.add(channel_group)
            session.flush()
            session.add(
                TgGroupAccount(
                    tenant_id=1,
                    group_id=channel_group.id,
                    account_id=account["id"],
                    can_send=True,
                    permission_label="管理员",
                )
            )
            session.commit()
        material = client.post(
            "/api/materials",
            headers=headers,
            json={
                "tenant_id": 1,
                "title": "消息发送图片",
                "material_type": "图片",
                "content": "https://trusted.example.com/message.png",
                "tags": "pytest",
                "tg_cache_peer_id": "cache-peer",
                "tg_cache_message_id": "101",
            },
        ).json()
        assert material["delivery_mode"] == "download_reupload"
        assert material["cache_ready_status"] == "not_cached"
        assert material["asset_fingerprint"]
        with SessionLocal() as session:
            db_material = session.get(Material, material["id"])
            db_material.cache_ready_status = "ready"
            db_material.tg_cache_account_id = account["id"]
            db_material.tg_cache_peer_id = "cache-peer"
            db_material.tg_cache_message_id = "101"
            session.commit()
        channel_task = client.post(
            "/api/message-send-tasks",
            headers=headers,
            json={
                "account_id": account["id"],
                "target_type": "channel",
                "operation_target_id": channel_target["id"],
                "content": "频道配文",
                "message_type": "图片",
                "material_id": material["id"],
                "dispatch_now": True,
            },
        )
        assert channel_task.status_code == 200, channel_task.text
        assert channel_task.json()["status"] == TaskStatus.SENT.value
        assert channel_task.json()["media_sent"] is True
        assert channel_task.json()["media_failure_reason"] == ""
        assert send_calls[-1]["peer_id"] == channel_target["tg_peer_id"]
        assert send_calls[-1]["segments"] == [("图片", "tg-cache://cache-peer/101", "频道配文")]

        sticker = client.post(
            "/api/materials",
            headers=headers,
            json={
                "tenant_id": 1,
                "title": "普通图片伪表情包",
                "material_type": "表情包",
                "content": "https://trusted.example.com/sticker.png",
                "emoji_asset_kind": "image_meme",
                "tg_cache_peer_id": "cache-peer",
                "tg_cache_message_id": "102",
            },
        ).json()
        assert sticker["cache_ready_status"] == "not_cached"
        with SessionLocal() as session:
            db_sticker = session.get(Material, sticker["id"])
            db_sticker.cache_ready_status = "ready"
            db_sticker.tg_cache_account_id = account["id"]
            db_sticker.tg_cache_peer_id = "cache-peer"
            db_sticker.tg_cache_message_id = "102"
            session.commit()
        sticker_task = client.post(
            "/api/message-send-tasks",
            headers=headers,
            json={
                "account_id": account["id"],
                "target_type": "private",
                "target_peer_id": f"@{contact['username']}",
                "content": "",
                "message_type": "表情包",
                "material_id": sticker["id"],
                "dispatch_now": True,
            },
        )
        assert sticker_task.status_code == 200, sticker_task.text
        assert sticker_task.json()["status"] == TaskStatus.SENT.value
        assert sticker_task.json()["media_sent"] is True
        assert send_calls[-1]["segments"] == [("表情包", "tg-cache://cache-peer/102", "")]

        manual_ready_material = client.post(
            "/api/materials",
            headers=headers,
            json={
                "tenant_id": 1,
                "title": "人工 ready 图片",
                "material_type": "图片",
                "content": "https://trusted.example.com/manual-ready.png",
                "cache_ready_status": "ready",
            },
        ).json()
        assert manual_ready_material["cache_ready_status"] == "not_cached"
        manual_ready_task = client.post(
            "/api/message-send-tasks",
            headers=headers,
            json={
                "account_id": account["id"],
                "target_type": "private",
                "target_peer_id": f"@{contact['username']}",
                "message_type": "图片",
                "material_id": manual_ready_material["id"],
                "dispatch_now": True,
            },
        )
        assert manual_ready_task.status_code == 400
        assert "素材缓存不可用" in manual_ready_task.text

        invalid_combo = client.post(
            "/api/materials",
            headers=headers,
            json={
                "tenant_id": 1,
                "title": "绕过缓存组合消息",
                "material_type": "组合消息",
                "content": json.dumps([{"type": "图片", "source": "https://trusted.example.com/bypass.png"}]),
            },
        ).json()
        invalid_combo_task = client.post(
            "/api/message-send-tasks",
            headers=headers,
            json={
                "account_id": account["id"],
                "target_type": "private",
                "target_peer_id": f"@{contact['username']}",
                "message_type": "组合消息",
                "material_id": invalid_combo["id"],
                "dispatch_now": True,
            },
        )
        assert invalid_combo_task.status_code == 400
        assert "组合消息媒体段必须引用已缓存素材" in invalid_combo_task.text

        valid_combo = client.post(
            "/api/materials",
            headers=headers,
            json={
                "tenant_id": 1,
                "title": "素材引用组合消息",
                "material_type": "组合消息",
                "content": json.dumps(["组合前置文本", {"type": "图片", "material_id": material["id"], "caption": "组合图片"}]),
            },
        ).json()
        valid_combo_task = client.post(
            "/api/message-send-tasks",
            headers=headers,
            json={
                "account_id": account["id"],
                "target_type": "private",
                "target_peer_id": f"@{contact['username']}",
                "content": "组合开头",
                "message_type": "组合消息",
                "material_id": valid_combo["id"],
                "dispatch_now": True,
            },
        )
        assert valid_combo_task.status_code == 200, valid_combo_task.text
        assert valid_combo_task.json()["media_sent"] is True
        assert send_calls[-1]["segments"] == [
            ("文本", None, ""),
            ("文本", None, ""),
            ("图片", "tg-cache://cache-peer/101", "组合图片"),
        ]

        flood_wait_material = client.post(
            "/api/materials",
            headers=headers,
            json={
                "tenant_id": 1,
                "title": "FloodWait 图片",
                "material_type": "图片",
                "content": "https://trusted.example.com/flood-wait.png",
                "cache_ready_status": "flood_wait",
            },
        ).json()
        flood_wait_task = client.post(
            "/api/message-send-tasks",
            headers=headers,
            json={
                "account_id": account["id"],
                "target_type": "channel",
                "operation_target_id": channel_target["id"],
                "content": "不能现场等待",
                "message_type": "图片",
                "material_id": flood_wait_material["id"],
                "dispatch_now": True,
            },
        )
        assert flood_wait_task.status_code == 400
        assert "素材缓存不可用" in flood_wait_task.text

        stale_cache_material = client.post(
            "/api/materials",
            headers=headers,
            json={
                "tenant_id": 1,
                "title": "运行期失效图片",
                "material_type": "图片",
                "content": "https://trusted.example.com/stale.png",
                "tg_cache_peer_id": "cache-peer",
                "tg_cache_message_id": "103",
            },
        ).json()
        assert stale_cache_material["cache_ready_status"] == "not_cached"
        with SessionLocal() as session:
            db_stale = session.get(Material, stale_cache_material["id"])
            db_stale.cache_ready_status = "ready"
            db_stale.tg_cache_account_id = account["id"]
            db_stale.tg_cache_peer_id = "cache-peer"
            db_stale.tg_cache_message_id = "103"
            session.commit()
        stale_cache_task = client.post(
            "/api/message-send-tasks",
            headers=headers,
            json={
                "account_id": account["id"],
                "target_type": "channel",
                "operation_target_id": channel_target["id"],
                "content": "缓存稍后失效",
                "message_type": "图片",
                "material_id": stale_cache_material["id"],
                "dispatch_now": False,
            },
        )
        assert stale_cache_task.status_code == 200, stale_cache_task.text
        with SessionLocal() as session:
            db_stale = session.get(Material, stale_cache_material["id"])
            db_stale.cache_ready_status = "flood_wait"
            session.commit()
        stale_cache_dispatch = client.post(f"/api/message-tasks/{stale_cache_task.json()['id']}/dispatch", headers=headers)
        assert stale_cache_dispatch.status_code == 200, stale_cache_dispatch.text
        stale_cache_body = stale_cache_dispatch.json()
        assert stale_cache_body["status"] == TaskStatus.FAILED.value
        assert stale_cache_body["media_sent"] is False
        assert stale_cache_body["media_failure_reason"] == "cache_account_flood_wait"
        assert stale_cache_body["failure_type"] == "cache_account_flood_wait"

        batch_start = (datetime.now(UTC) + timedelta(minutes=10)).replace(microsecond=0)
        mixed_batch = client.post(
            "/api/message-send-tasks/batch",
            headers=headers,
            json={
                "account_id": account["id"],
                "targets": [
                    {
                        "target_type": "private",
                        "target_peer_id": f"@{contact['username']}",
                        "target_display": contact["display_name"],
                    },
                    {"target_type": "group", "group_id": group["id"]},
                    {"target_type": "channel", "operation_target_id": channel_target["id"]},
                ],
                "content": "混合定时消息",
                "message_type": "文本",
                "dispatch_now": False,
                "scheduled_at": batch_start.isoformat(),
            },
        )
        assert mixed_batch.status_code == 200, mixed_batch.text
        batch_body = mixed_batch.json()
        assert [item["target_type"] for item in batch_body] == ["private", "group", "channel"]
        assert [item["status"] for item in batch_body] == [TaskStatus.QUEUED.value] * 3
        scheduled_values = [datetime.fromisoformat(item["scheduled_at"]) for item in batch_body]
        assert scheduled_values[0].replace(tzinfo=UTC) == batch_start
        assert (scheduled_values[1] - scheduled_values[0]).total_seconds() == 30
        assert (scheduled_values[2] - scheduled_values[1]).total_seconds() == 30
        assert batch_body[2]["target_display"] == channel_target["title"]

        missing_targets = client.post(
            "/api/message-send-tasks/batch",
            headers=headers,
            json={
                "account_id": account["id"],
                "targets": [],
                "content": "没有目标",
                "message_type": "文本",
            },
        )
        assert missing_targets.status_code == 422

        before_jitter_calls = len(send_calls)
        jittered = client.post(
            "/api/message-send-tasks",
            headers=headers,
            json={
                "account_id": account["id"],
                "target_type": "private",
                "target_peer_id": f"@{contact['username']}",
                "content": "抖动消息",
                "message_type": "文本",
                "jitter_min_seconds": 10,
                "jitter_max_seconds": 20,
                "dispatch_now": True,
            },
        )
        assert jittered.status_code == 200, jittered.text
        jitter_body = jittered.json()
        assert jitter_body["status"] == TaskStatus.QUEUED.value
        assert 10 <= jitter_body["planned_delay_seconds"] <= 20
        assert len(send_calls) == before_jitter_calls

        missing_material = client.post(
            "/api/message-send-tasks",
            headers=headers,
            json={
                "account_id": account["id"],
                "target_type": "channel",
                "operation_target_id": channel_target["id"],
                "message_type": "图片",
                "content": "",
            },
        )
        assert missing_material.status_code == 400
        assert "素材" in missing_material.text

        client.delete(f"/api/tg-accounts/{account['id']}", headers=headers)
        deleted_account_task = client.post(
            "/api/message-send-tasks",
            headers=headers,
            json={
                "account_id": account["id"],
                "target_type": "private",
                "target_peer_id": f"@{contact['username']}",
                "content": "删除账号不能发",
                "message_type": "文本",
            },
        )
        assert deleted_account_task.status_code == 400
        assert "未删除" in deleted_account_task.text


def test_ai_operation_failure_notifies_admin(monkeypatch):
    skip_legacy_task_center_flow()
    sent: list[tuple[str, str, str]] = []

    def fake_bot(token: str, chat_id: str, text: str):
        sent.append((token, chat_id, text))
        return NotificationResult(True, "sent")

    monkeypatch.setattr("app.services.notifications.send_telegram_bot_message", fake_bot)
    monkeypatch.setattr("app.services.operations.ai_gateway.generate_drafts", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("AI provider down")))
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _ = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            session.commit()

        configured = client.patch(
            "/api/tenant-notification-settings",
            headers=headers,
            json={"notify_ai_failures_enabled": True, "admin_chat_id": "12345", "telegram_bot_token": "bot-token"},
        )
        assert configured.status_code == 200, configured.text
        assert configured.json()["telegram_bot_configured"] is True

        target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "target_type": "group",
                "tg_peer_id": f"pytest-ai-fail-{uuid4().hex[:8]}",
                "title": "AI失败目标",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        task = client.post(
            "/api/operation-tasks",
            headers=headers,
            json={
                "task_type": "MESSAGE_SEND",
                "target_id": target["id"],
                "title": "AI消息失败",
                "content": "围绕活动自然发一条",
                "content_mode": "ai",
                "account_ids": [account["id"]],
                "quantity": 3,
            },
        )
        assert task.status_code == 200, task.text
        body = task.json()
        assert body["status"] == TaskStatus.FAILED.value
        assert "AI" in body["failure_detail"] or "供应商" in body["failure_detail"]
        assert sent and sent[0][1] == "12345"
        assert "AI 运营任务失败" in sent[0][2]

        with SessionLocal() as session:
            attempts = session.query(OperationTaskAttempt).filter_by(task_id=body["id"]).all()
            assert len(attempts) == 1
            assert attempts[0].status == TaskStatus.FAILED.value


def test_ai_failure_notification_error_is_non_blocking(monkeypatch):
    skip_legacy_task_center_flow()
    monkeypatch.setattr(
        "app.services.notifications.send_telegram_bot_message",
        lambda *args, **kwargs: NotificationResult(False, "bot down"),
    )
    monkeypatch.setattr("app.services.operations.ai_gateway.generate_drafts", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("AI provider down")))
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _ = ensure_test_workspace(client, headers)
        client.patch(
            "/api/tenant-notification-settings",
            headers=headers,
            json={"notify_ai_failures_enabled": True, "admin_chat_id": "12345", "telegram_bot_token": "bot-token"},
        )
        target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "target_type": "group",
                "tg_peer_id": f"pytest-bot-fail-{uuid4().hex[:8]}",
                "title": "通知失败目标",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        task = client.post(
            "/api/operation-tasks",
            headers=headers,
            json={
                "task_type": "MESSAGE_SEND",
                "target_id": target["id"],
                "title": "AI消息失败但通知失败",
                "content": "自然发一条",
                "content_mode": "ai",
                "account_ids": [account["id"]],
                "quantity": 1,
            },
        )
        assert task.status_code == 200, task.text
        assert task.json()["status"] == TaskStatus.FAILED.value
        with SessionLocal() as session:
            assert session.query(AuditLog).filter_by(action="AI失败通知失败").count() >= 1


def test_ai_operation_retry_replans_after_generation_failure(monkeypatch):
    skip_legacy_task_center_flow()
    generation_should_fail = {"value": True}

    def fake_generate(*args, **kwargs):
        count = kwargs["count"]
        if generation_should_fail["value"]:
            raise RuntimeError("AI provider down")
        return [f"重试生成内容 {index + 1}" for index in range(count)]

    monkeypatch.setattr("app.services.operations._generate_operation_contents", fake_generate)
    monkeypatch.setattr("app.services.operations.gateway.send_message_to_target", lambda *args, **kwargs: SendResult(True, remote_message_id="retry-sent"))

    with TestClient(app) as client:
        headers = auth_headers(client)
        account, _ = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            session.commit()
        target = client.post(
            "/api/operation-targets",
            headers=headers,
            json={
                "target_type": "group",
                "tg_peer_id": f"pytest-retry-ai-{uuid4().hex[:8]}",
                "title": "AI重试目标",
                "can_send": True,
                "auth_status": "已授权运营",
            },
        ).json()
        created = client.post(
            "/api/operation-tasks",
            headers=headers,
            json={
                "task_type": "MESSAGE_SEND",
                "target_id": target["id"],
                "title": "AI消息重试",
                "content": "重试时重新生成",
                "content_mode": "ai",
                "account_ids": [account["id"]],
                "quantity": 2,
                "quantity_jitter_ratio": 0,
            },
        ).json()
        assert created["status"] == TaskStatus.FAILED.value

        generation_should_fail["value"] = False
        retried = client.post(f"/api/operation-tasks/{created['id']}/retry", headers=headers)
        assert retried.status_code == 200, retried.text
        body = retried.json()
        assert body["status"] == TaskStatus.COMPLETED.value
        assert body["completed_count"] == 2
        with SessionLocal() as session:
            attempts = session.query(OperationTaskAttempt).filter_by(task_id=created["id"]).order_by(OperationTaskAttempt.id.asc()).all()
            assert len(attempts) == 2
            assert all(attempt.account_id == account["id"] for attempt in attempts)
            assert [attempt.content for attempt in attempts] == ["重试生成内容 1", "重试生成内容 2"]


def test_group_policy_enforcement_material_usage_and_reports(monkeypatch):
    skip_legacy_task_center_flow()
    monkeypatch.setattr("app.services._common.gateway.send_message", lambda *args, **kwargs: SendResult(True, remote_message_id="pytest-sent"))
    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        client.post(f"/api/groups/{group['id']}/authorize", headers=headers, json={"auth_status": "已授权运营"})
        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            link = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=account["id"]).first()
            if not link:
                session.add(TgGroupAccount(tenant_id=1, group_id=group["id"], account_id=account["id"], can_send=True, permission_label="普通成员"))
                session.flush()
                link = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=account["id"]).first()
            link.can_send = True
            target_group = session.query(type(link.group)).filter_by(id=group["id"]).first()
            target_group.daily_limit = 999
            target_group.group_cooldown_seconds = 0
            target_group.account_cooldown_seconds = 0
            target_group.banned_words = ""
            target_group.link_whitelist = ""
            target_group.require_review = True
            session.commit()

        material = client.post(
            "/api/materials",
            headers=headers,
            json={"tenant_id": 1, "title": "pytest 图片素材", "material_type": "图片", "content": "https://trusted.example.com/a.png", "tags": "pytest"},
        ).json()
        campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": group["id"],
                "title": "策略发送成功",
                "campaign_type": "定时活跃任务",
                "topic": "策略发送",
                "material_ids": str(material["id"]),
                "jitter_min_seconds": 0,
                "jitter_max_seconds": 0,
                "batch_interval_seconds": 0,
                "respect_send_window": False,
            },
        ).json()
        draft = client.post(f"/api/campaigns/{campaign['id']}/generate-drafts", headers=headers, json={"count": 1}).json()[0]
        task = client.post(f"/api/ai-drafts/{draft['id']}/approve", headers=headers, json={"actor": "策略测试"}).json()
        dispatched = client.post(f"/api/message-tasks/{task['id']}/dispatch", headers=headers).json()
        assert dispatched["status"] == "已发送"
        materials = client.get("/api/materials", headers=headers).json()
        used_material = next(item for item in materials if item["id"] == material["id"])
        assert used_material["usage_count"] >= 1

        with SessionLocal() as session:
            target_group = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=account["id"]).first().group
            target_group.daily_limit = 1
            session.commit()

        second_campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": group["id"],
                "title": "策略发送超限",
                "campaign_type": "定时活跃任务",
                "topic": "策略超限",
                "jitter_min_seconds": 0,
                "jitter_max_seconds": 0,
                "batch_interval_seconds": 0,
                "respect_send_window": False,
            },
        ).json()
        second_draft = client.post(f"/api/campaigns/{second_campaign['id']}/generate-drafts", headers=headers, json={"count": 1}).json()[0]
        second_task = client.post(f"/api/ai-drafts/{second_draft['id']}/approve", headers=headers, json={"actor": "策略测试"}).json()
        second_dispatched = client.post(f"/api/message-tasks/{second_task['id']}/dispatch", headers=headers).json()
        assert second_dispatched["status"] == "失败"
        assert "当日发送已达上限" in (second_dispatched["failure_detail"] or "")

        with SessionLocal() as session:
            target_group = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=account["id"]).first().group
            target_group.daily_limit = 10
            target_group.banned_words = "禁词"
            target_group.link_whitelist = "trusted.example.com"
            session.commit()

        banned_campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": group["id"],
                "title": "禁词策略",
                "campaign_type": "定时活跃任务",
                "topic": "禁词策略",
                "jitter_min_seconds": 0,
                "jitter_max_seconds": 0,
                "batch_interval_seconds": 0,
                "respect_send_window": False,
            },
        ).json()
        banned_draft = client.post(f"/api/campaigns/{banned_campaign['id']}/generate-drafts", headers=headers, json={"count": 1}).json()[0]
        client.patch(f"/api/ai-drafts/{banned_draft['id']}", headers=headers, json={"content": "这里包含禁词"})
        banned_task = client.post(f"/api/ai-drafts/{banned_draft['id']}/approve", headers=headers, json={"actor": "策略测试"}).json()
        banned_dispatched = client.post(f"/api/message-tasks/{banned_task['id']}/dispatch", headers=headers).json()
        assert banned_dispatched["status"] == "失败"
        assert banned_dispatched["failure_type"] == "内容违规"

        link_material = client.post(
            "/api/materials",
            headers=headers,
            json={"tenant_id": 1, "title": "外链素材", "material_type": "链接", "content": "https://evil.example.com/wrong", "tags": "pytest"},
        ).json()
        link_campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": group["id"],
                "title": "白名单策略",
                "campaign_type": "定时活跃任务",
                "topic": "白名单策略",
                "material_ids": str(link_material["id"]),
                "jitter_min_seconds": 0,
                "jitter_max_seconds": 0,
                "batch_interval_seconds": 0,
                "respect_send_window": False,
            },
        ).json()
        link_draft = client.post(f"/api/campaigns/{link_campaign['id']}/generate-drafts", headers=headers, json={"count": 1}).json()[0]
        with SessionLocal() as session:
            db_draft = session.get(AiDraft, link_draft["id"])
            db_draft.material_id = link_material["id"]
            session.commit()
        link_task = client.post(f"/api/ai-drafts/{link_draft['id']}/approve", headers=headers, json={"actor": "策略测试"}).json()
        with SessionLocal() as session:
            db_task = session.get(MessageTask, link_task["id"])
            db_task.material_id = link_material["id"]
            db_task.message_type = "链接"
            session.commit()
        link_dispatched = client.post(f"/api/message-tasks/{link_task['id']}/dispatch", headers=headers).json()
        assert link_dispatched["status"] == "失败"
        assert "白名单" in (link_dispatched["failure_detail"] or "")

        report = client.get("/api/reports", headers=headers).json()
        assert report["groups"]["daily_messages"] >= 1
        assert report["tasks"]["avg_delay_seconds"] >= 0


def test_operation_metrics_prd_reports_and_export_endpoints():
    with TestClient(app) as client:
        headers = auth_headers(client)
        suffix = uuid4().hex[:8]
        viewer_response = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "name": f"运营数据只读{suffix}",
                "email": f"usage_viewer_{suffix}@example.local",
                "password": "usage123",
                "role": "后台用户",
                "role_template": "只读观察员",
                "permissions": ["overview.view", "usage.view"],
                "menu_permissions": ["overview.view", "usage.view"],
            },
        )
        assert viewer_response.status_code == 200, viewer_response.text
        viewer_headers = auth_headers(client, viewer_response.json()["email"], "usage123")
        exporter_response = client.post(
            "/api/admin/users",
            headers=headers,
            json={
                "name": f"运营数据导出{suffix}",
                "email": f"usage_exporter_{suffix}@example.local",
                "password": "usage123",
                "role": "后台用户",
                "role_template": "运营管理员",
                "permissions": ["overview.view", "usage.view", "usage.export"],
                "menu_permissions": ["overview.view", "usage.view", "usage.export"],
            },
        )
        assert exporter_response.status_code == 200, exporter_response.text
        exporter_headers = auth_headers(client, exporter_response.json()["email"], "usage123")

        report = client.get("/api/operation-metrics/reports", headers=viewer_headers)
        assert report.status_code == 200, report.text
        report_body = report.json()
        assert {"accounts", "groups", "tasks", "tenant"}.issubset(report_body)

        denied_export = client.post("/api/operation-metrics/export", headers=viewer_headers, json={"reason": "只读不能导出"})
        assert denied_export.status_code == 403
        assert denied_export.json()["permission"] == "usage.export"

        missing_reason = client.post("/api/operation-metrics/export", headers=exporter_headers, json={"reason": "   "})
        assert missing_reason.status_code == 422

        exported = client.post("/api/operation-metrics/export", headers=exporter_headers, json={"reason": "pytest 运营指标导出"})
        assert exported.status_code == 200, exported.text
        assert "text/csv" in exported.headers["content-type"]
        assert "operation-metrics.csv" in exported.headers["content-disposition"]
        assert "section,key,value" in exported.text

        audit_logs = client.get("/api/audit-logs?target_type=operation_metrics&target_id=export", headers=headers)
        assert audit_logs.status_code == 200, audit_logs.text
        assert any("pytest 运营指标导出" in item["detail"] for item in audit_logs.json())


def test_account_deleted_during_dispatch_does_not_send(monkeypatch):
    skip_legacy_task_center_flow()
    send_calls = {"count": 0}

    def fake_credentials(session, account, *, assign_if_missing=False):
        from app.services.accounts import soft_delete_account

        soft_delete_account(session, account.id, actor="pytest", reason="dispatch race")
        return DeveloperAppCredentials(app_id=1, api_id=12345, api_hash="test_hash", credentials_version=1)

    def fake_send(*args, **kwargs):
        send_calls["count"] += 1
        return SendResult(True, remote_message_id="should-not-send")

    monkeypatch.setattr("app.services.messages.credentials_for_account", fake_credentials)
    monkeypatch.setattr("app.services.messages.gateway.send_message", fake_send)

    with TestClient(app) as client:
        headers = auth_headers(client)
        account, group = ensure_test_workspace(client, headers)
        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            link = session.query(TgGroupAccount).filter_by(group_id=group["id"], account_id=account["id"]).first()
            link.can_send = True
            target_group = link.group
            target_group.daily_limit = 999
            target_group.group_cooldown_seconds = 0
            target_group.account_cooldown_seconds = 0
            target_group.require_review = True
            session.commit()

        campaign = client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "tenant_id": 1,
                "group_id": group["id"],
                "title": "删除期间不发送",
                "campaign_type": "定时活跃任务",
                "topic": "删除账号",
                "target_group_ids": [group["id"]],
                "selected_account_ids_by_group": {str(group["id"]): [account["id"]]},
                "jitter_min_seconds": 0,
                "jitter_max_seconds": 0,
                "batch_interval_seconds": 0,
                "respect_send_window": False,
            },
        ).json()
        draft = client.post(f"/api/campaigns/{campaign['id']}/generate-drafts", headers=headers, json={"count": 1}).json()[0]
        task = client.post(f"/api/ai-drafts/{draft['id']}/approve", headers=headers, json={"actor": "pytest"}).json()

        dispatched = client.post(f"/api/message-tasks/{task['id']}/dispatch", headers=headers)
        assert dispatched.status_code == 200, dispatched.text
        body = dispatched.json()
        assert body["status"] == TaskStatus.FAILED.value
        assert body["failure_type"] == FailureType.ACCOUNT_UNAVAILABLE.value
        assert send_calls["count"] == 0

        with SessionLocal() as session:
            db_task = session.get(MessageTask, task["id"])
            assert db_task.sent_at is None
            assert session.get(TgAccount, account["id"]).deleted_at is not None


def test_archive_async_and_extended_sync_types():
    settings = get_settings()
    original_mode = settings.tg_gateway_mode
    object.__setattr__(settings, "tg_gateway_mode", "telethon")
    try:
        with TestClient(app) as client:
            headers = auth_headers(client)
            account, group = ensure_test_workspace(client, headers)
            with SessionLocal() as session:
                db_account = session.get(TgAccount, account["id"])
                db_account.status = AccountStatus.ACTIVE.value
                session.commit()

            archive = client.post(
                "/api/archives",
                headers=headers,
                json={"tenant_id": 1, "group_id": group["id"], "title": "异步归档测试"},
            ).json()
            assert archive["status"] == "排队中"
            assert archive["sync_mode"] == "async"

            drained = client.post("/api/worker/drain-once", headers=headers, json={"reason": "测试手动 drain"}).json()
            assert drained["processed"] >= 1

            detail = client.get(f"/api/archives/{archive['id']}", headers=headers).json()
            assert detail["archive"]["status"] == "已完成"
            assert detail["invite_candidates"]

            with SessionLocal() as session:
                source_account = session.get(TgAccount, account["id"])
                session.add(TgAccountSyncRecord(tenant_id=1, account_id=account["id"], sync_type="health", trigger_source="pytest", status="排队中", scheduled_at=source_account.created_at, created_at=source_account.created_at))
                session.add(TgAccountSyncRecord(tenant_id=1, account_id=account["id"], sync_type="profile_pull", trigger_source="pytest", status="排队中", scheduled_at=source_account.created_at, created_at=source_account.created_at))
                session.commit()

            drained_again = client.post("/api/worker/drain-once", headers=headers, json={"reason": "测试手动 drain"}).json()
            assert drained_again["processed"] >= 1
            sync_records = client.get(f"/api/tg-accounts/{account['id']}/sync-records", headers=headers).json()
            assert {"health", "profile_pull"}.issubset({record["sync_type"] for record in sync_records})
    finally:
        object.__setattr__(settings, "tg_gateway_mode", original_mode)
