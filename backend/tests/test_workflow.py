from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.ai_gateway import AiGenerationResult, AiUsage, mock_candidates
from app.config import get_settings
from app.auth import get_challenge_target
from app.database import SessionLocal
from app.main import app
from app.gateways import SendResult
from app.models import AccountStatus, AiDraft, AiUsageLedger, AuditLog, Campaign, DeveloperAppHealthStatus, FailureType, ManualOperationRecord, Material, MessageTask, OperationTaskAttempt, TaskStatus, TelegramDeveloperApp, TgAccount, TgAccountSyncRecord, TgGroupAccount, TgLoginFlow
from app.services.notifications import NotificationResult
from fastapi.testclient import TestClient
from sqlalchemy import inspect


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


def ensure_developer_app(client: TestClient, headers: dict[str, str]) -> dict:
    apps = client.get("/api/developer-apps", headers=headers).json()
    healthy = [app for app in apps if app["is_active"] and app["health_status"] == "健康"]
    if healthy:
        return healthy[0]
    suffix = int(uuid4().int % 100000)
    response = client.post(
        "/api/developer-apps",
        headers=headers,
        json={
            "app_name": f"测试开发者应用 {suffix}",
            "api_id": 700000 + suffix,
            "api_hash": f"test_api_hash_secret_{suffix}",
            "max_accounts": 50,
            "notes": "pytest",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def ensure_test_workspace(client: TestClient, headers: dict[str, str]) -> tuple[dict, dict]:
    ensure_developer_app(client, headers)
    suffix = uuid4().hex[:8]
    account = client.post(
        "/api/tg-accounts",
        headers=headers,
        json={
            "tenant_id": 1,
            "display_name": f"本地测试账号 {suffix}",
            "username": f"local_test_{suffix}",
            "phone_number": f"+86138{int(uuid4().int % 100000000):08d}",
        },
    ).json()

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
    return account, group


def test_clean_seed_requires_config_before_account_create():
    with TestClient(app) as client:
        headers = auth_headers(client)
        runtime = client.get("/api/config/runtime").json()
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


def test_campaign_draft_approval_and_dispatch_flow():
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
        runtime = client.get("/api/config/runtime").json()
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

        checked = client.post(f"/api/tg-accounts/{account['id']}/health-check", headers=headers).json()
        assert checked["status"] in {"在线", "受限", "需重新登录"}

        authorized = client.post(f"/api/groups/{group['id']}/authorize", headers=headers, json={"auth_status": "已授权运营"}).json()
        assert authorized["auth_status"] == "已授权运营"


def test_approve_all_retry_and_archive_detail_flow():
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

        drained = client.post("/api/worker/drain-once", headers=headers).json()
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


def test_auth_single_admin_and_default_operation_space():
    with TestClient(app) as client:
        headers = auth_headers(client)
        me = client.get("/api/auth/me", headers=headers).json()
        assert me["tenant_id"] == 1
        assert me["role"] == "系统管理员"
        assert "subscription_status" not in me
        assert login_response(client, "ops@bootstrap.local", "ops123").status_code == 401

        response = client.get("/api/tg-accounts?tenant_id=999", headers=headers)
        assert response.status_code == 200

        assert client.get("/api/tg-accounts").status_code == 401


def test_legacy_authorization_endpoints_and_tables_are_removed():
    with TestClient(app) as client:
        headers = auth_headers(client)
        old_endpoints = [
            ("post", "/api/auth/register", {"email": "x@example.local", "password": "secret"}),
            ("post", "/api/subscription/redeem", {"code": "NOPE"}),
            ("get", "/api/admin/users", None),
            ("get", "/api/admin/activation-codes", None),
            ("post", "/api/admin/activation-codes", {"plan_type": "monthly", "quantity": 1}),
            ("get", "/api/admin/subscription-plans", None),
            ("post", "/api/admin/subscription-plans", {"plan_type": "monthly", "name": "Legacy"}),
        ]
        for method, path, payload in old_endpoints:
            request = getattr(client, method)
            response = request(path, headers=headers, json=payload) if payload is not None and method != "get" else request(path, headers=headers)
            assert response.status_code == 404, f"{path} should be removed"

        with SessionLocal() as session:
            inspector = inspect(session.bind)
            for table_name in ["app_users", "activation_codes", "subscription_plans", "user_token_ledgers"]:
                assert not inspector.has_table(table_name)


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
    with TestClient(app) as client:
        headers = auth_headers(client)
        runtime = client.get("/api/config/runtime").json()
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
        drained = client.post("/api/worker/drain-once", headers=headers).json()
        assert drained["processed"] == 0


def test_ai_real_provider_records_campaign_usage_without_user_token_balance(monkeypatch):
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

        codes = client.post(f"/api/tg-accounts/{account['id']}/verification-codes/poll", headers=headers).json()
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
            session.commit()

        detail = client.get(f"/api/tg-accounts/{account['id']}/detail", headers=headers).json()
        risks = detail["risk_diagnostics"]
        assert detail["stats"]["risk_diagnostics"] >= 2
        assert detail["stats"]["high_risk_diagnostics"] >= 1
        assert any(risk["code"] == "ACCOUNT_STATUS" and risk["title"] == "账号受限" for risk in risks)
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

        drained = client.post("/api/worker/drain-once", headers=headers).json()
        assert drained["processed"] >= 1
        detail = client.get(f"/api/tg-accounts/{account['id']}/detail", headers=headers).json()
        assert detail["account"]["profile_sync_status"] == "已同步"
        assert detail["profile_sync_records"][0]["status"] == "已同步"

        retry = client.post(f"/api/tg-accounts/{account['id']}/profile-sync/retry", headers=headers).json()
        assert retry["status"] == "排队中"


def test_account_pool_clone_plan_and_verification_tasks():
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
        drained = client.post("/api/worker/drain-once", headers=headers).json()
        assert drained["processed"] >= 1


def test_multi_group_recommendation_and_approval_expands_tasks():
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


def test_ai_operation_failure_notifies_admin(monkeypatch):
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

            drained = client.post("/api/worker/drain-once", headers=headers).json()
            assert drained["processed"] >= 1

            detail = client.get(f"/api/archives/{archive['id']}", headers=headers).json()
            assert detail["archive"]["status"] == "已完成"
            assert detail["invite_candidates"]

            with SessionLocal() as session:
                source_account = session.get(TgAccount, account["id"])
                session.add(TgAccountSyncRecord(tenant_id=1, account_id=account["id"], sync_type="health", trigger_source="pytest", status="排队中", scheduled_at=source_account.created_at, created_at=source_account.created_at))
                session.add(TgAccountSyncRecord(tenant_id=1, account_id=account["id"], sync_type="profile_pull", trigger_source="pytest", status="排队中", scheduled_at=source_account.created_at, created_at=source_account.created_at))
                session.commit()

            drained_again = client.post("/api/worker/drain-once", headers=headers).json()
            assert drained_again["processed"] >= 1
            sync_records = client.get(f"/api/tg-accounts/{account['id']}/sync-records", headers=headers).json()
            assert {"health", "profile_pull"}.issubset({record["sync_type"] for record in sync_records})
    finally:
        object.__setattr__(settings, "tg_gateway_mode", original_mode)
