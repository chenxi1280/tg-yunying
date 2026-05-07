from uuid import uuid4

from app.config import get_settings
from app.auth import get_challenge_target
from app.database import SessionLocal
from app.main import app
from app.gateways import SendResult
from app.models import AccountStatus, AiDraft, DeveloperAppHealthStatus, Material, MessageTask, TelegramDeveloperApp, TgAccount, TgAccountSyncRecord, TgContact, TgGroupAccount
from fastapi.testclient import TestClient


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
        flow = client.post(f"/api/tg-accounts/{account['id']}/login/start", headers=headers, json={"method": "code"}).json()
        assert flow["status"] == "等待验证码"
        assert flow["code_preview"]

        account = client.post(f"/api/tg-accounts/{account['id']}/login/verify", headers=headers, json={"code": flow["code_preview"]}).json()
        assert account["status"] == "在线"
        sync_records = client.get(f"/api/tg-accounts/{account['id']}/sync-records", headers=headers).json()
        assert {"groups", "contacts", "codes"}.issubset({record["sync_type"] for record in sync_records})


def test_runtime_login_flows_health_and_group_authorize():
    with TestClient(app) as client:
        headers = auth_headers(client)
        runtime = client.get("/api/config/runtime").json()
        assert runtime["tg_gateway_mode"] in {"mock", "telethon"}

        account, group = ensure_test_workspace(client, headers)
        client.post(f"/api/tg-accounts/{account['id']}/login/start", headers=headers, json={"method": "qr"})
        flows = client.get(f"/api/tg-accounts/{account['id']}/login-flows", headers=headers).json()
        assert flows
        qr_account = client.post(f"/api/tg-accounts/{account['id']}/login/qr/check", headers=headers).json()
        assert qr_account["status"] == "在线"

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


def test_auth_and_tenant_isolation():
    with TestClient(app) as client:
        headers = auth_headers(client, "ops@bootstrap.local", "ops123")
        me = client.get("/api/auth/me", headers=headers).json()
        assert me["tenant_id"] == 1

        response = client.get("/api/tg-accounts?tenant_id=999", headers=headers)
        assert response.status_code == 403

        assert client.get("/api/tg-accounts").status_code == 401


def test_activation_code_generation_search_pagination_and_disable():
    with TestClient(app) as client:
        headers = auth_headers(client)
        ops_headers = auth_headers(client, "ops@bootstrap.local", "ops123")
        batch_no = f"B{uuid4().hex[:8]}".upper()
        serial_prefix = f"VIP{uuid4().hex[:4]}".upper()

        created = client.post(
            "/api/admin/activation-codes",
            headers=headers,
            json={
                "plan_type": "monthly",
                "quantity": 5,
                "batch_no": batch_no,
                "serial_prefix": serial_prefix,
                "note": "pytest batch",
            },
        )
        assert created.status_code == 200, created.text
        codes = created.json()
        assert len(codes) == 5
        assert all(item["batch_no"] == batch_no for item in codes)
        assert all(item["serial_prefix"] == serial_prefix for item in codes)
        assert all(item["plan_type"] == "monthly" and item["duration_days"] == 30 for item in codes)
        assert all(item["code"].startswith(f"{serial_prefix}-{batch_no}-") for item in codes)

        first_page = client.get("/api/admin/activation-codes", headers=headers, params={"batch_no": batch_no, "page": 1, "page_size": 2}).json()
        assert first_page["total"] == 5
        assert first_page["page"] == 1
        assert first_page["page_size"] == 2
        assert len(first_page["items"]) == 2
        assert first_page["items"][0]["id"] > first_page["items"][1]["id"]

        by_plan = client.get("/api/admin/activation-codes", headers=headers, params={"batch_no": batch_no, "plan_type": "monthly"}).json()
        assert by_plan["total"] == 5
        by_keyword = client.get("/api/admin/activation-codes", headers=headers, params={"search": serial_prefix}).json()
        assert by_keyword["total"] >= 5

        disabled = client.post(f"/api/admin/activation-codes/{codes[0]['id']}/disable", headers=headers)
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["status"] == "disabled"
        disabled_redeem = client.post("/api/subscription/redeem", headers=ops_headers, json={"code": codes[0]["code"]})
        assert disabled_redeem.status_code == 400

        redeemed = client.post("/api/subscription/redeem", headers=ops_headers, json={"code": codes[1]["code"]})
        assert redeemed.status_code == 200, redeemed.text
        redeemed_disable = client.post(f"/api/admin/activation-codes/{codes[1]['id']}/disable", headers=headers)
        assert redeemed_disable.status_code == 400

        disabled_page = client.get("/api/admin/activation-codes", headers=headers, params={"batch_no": batch_no, "status": "disabled"}).json()
        assert disabled_page["total"] == 1
        unused_page = client.get("/api/admin/activation-codes", headers=headers, params={"batch_no": batch_no, "status": "unused"}).json()
        assert unused_page["total"] == 3
        by_redeemed_user = client.get("/api/admin/activation-codes", headers=headers, params={"batch_no": batch_no, "search": "ops@bootstrap.local"}).json()
        assert by_redeemed_user["total"] == 1
        assert by_redeemed_user["items"][0]["redeemed_user_email"] == "ops@bootstrap.local"

        denied_list = client.get("/api/admin/activation-codes", headers=ops_headers)
        denied_create = client.post("/api/admin/activation-codes", headers=ops_headers, json={"plan_type": "monthly", "quantity": 1})
        denied_disable = client.post(f"/api/admin/activation-codes/{codes[2]['id']}/disable", headers=ops_headers)
        assert denied_list.status_code == 403
        assert denied_create.status_code == 403
        assert denied_disable.status_code == 403


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

        ops_headers = auth_headers(client, "ops@bootstrap.local", "ops123")
        denied = client.post(
            "/api/developer-apps",
            headers=ops_headers,
            json={"app_name": "无权应用", "api_id": api_id + 1, "api_hash": "another_secret"},
        )
        assert denied.status_code == 403

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
            assert "phone_number" not in first_account
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

        ops_headers = auth_headers(client, "ops@bootstrap.local", "ops123")
        denied = client.get("/api/prompt-templates?tenant_id=999", headers=ops_headers)
        assert denied.status_code == 403


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


def test_ai_provider_write_requires_platform_admin():
    with TestClient(app) as client:
        ops_headers = auth_headers(client, "ops@bootstrap.local", "ops123")
        denied = client.post(
            "/api/ai-providers",
            headers=ops_headers,
            json={"provider_name": "Denied", "base_url": "mock://openai-compatible", "model_name": "x", "api_key": "secret"},
        )
        assert denied.status_code == 403


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


def test_tenant_quota_patch_and_enforcement():
    with TestClient(app) as client:
        headers = auth_headers(client)
        tenant = client.post(
            "/api/tenants",
            headers=headers,
            json={
                "name": f"quota-tenant-{uuid4().hex[:6]}",
                "plan_name": "配额测试",
                "account_quota": 1,
                "task_quota": 1,
            },
        ).json()

        first = client.post(
            "/api/tg-accounts",
            headers=headers,
            json={"tenant_id": tenant["id"], "display_name": "配额账号一", "phone_number": f"+86136{uuid4().int % 100000000:08d}"},
        )
        assert first.status_code == 200, first.text

        second = client.post(
            "/api/tg-accounts",
            headers=headers,
            json={"tenant_id": tenant["id"], "display_name": "配额账号二", "phone_number": f"+86135{uuid4().int % 100000000:08d}"},
        )
        assert second.status_code == 400
        assert "账号配额不足" in second.text

        account = first.json()
        with SessionLocal() as session:
            db_account = session.get(TgAccount, account["id"])
            db_account.status = AccountStatus.ACTIVE.value
            session.add(
                TgContact(
                    tenant_id=tenant["id"],
                    account_id=db_account.id,
                    peer_id="quota-peer-1",
                    display_name="Quota Contact",
                    username="quota_contact",
                    created_at=db_account.created_at,
                    last_synced_at=db_account.created_at,
                )
            )
            session.commit()

        first_task = client.post(
            f"/api/tg-accounts/{account['id']}/direct-message-tasks",
            headers=headers,
            json={"target_peer_id": "@quota_contact", "target_display": "Quota Contact", "content": "first quota task"},
        )
        assert first_task.status_code == 200, first_task.text

        second_task = client.post(
            f"/api/tg-accounts/{account['id']}/direct-message-tasks",
            headers=headers,
            json={"target_peer_id": "@quota_contact", "target_display": "Quota Contact", "content": "second quota task"},
        )
        assert second_task.status_code == 400
        assert "任务配额不足" in second_task.text

        updated = client.patch(
            f"/api/tenants/{tenant['id']}",
            headers=headers,
            json={"account_quota": 2, "task_quota": 2},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["task_quota"] == 2

        third = client.post(
            "/api/tg-accounts",
            headers=headers,
            json={"tenant_id": tenant["id"], "display_name": "配额账号二", "phone_number": f"+86134{uuid4().int % 100000000:08d}"},
        )
        assert third.status_code == 200, third.text

        third_task = client.post(
            f"/api/tg-accounts/{account['id']}/direct-message-tasks",
            headers=headers,
            json={"target_peer_id": "@quota_contact", "target_display": "Quota Contact", "content": "third quota task"},
        )
        assert third_task.status_code == 200, third_task.text


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
