from __future__ import annotations

from datetime import timedelta
import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.integrations.telegram.contracts import AccountAuthorizationSnapshot as RemoteAuthorizationSnapshot
from app.integrations.telegram.contracts import RemoteProfile
from app.models import AiProvider, AccountProxy, AccountStatus, Material, TelegramDeveloperApp, Tenant, TenantAiSetting, TgAccount, TgAccountAuthorization, TgAccountAuthorizationSnapshot, TgAccountSecurityBatch, TgAccountSecurityBatchItem, TgAccountSecuritySnapshot, TgVerificationCode
from app.schemas import TgAccountCreate
from app.schemas.account_security import AccountSecurityBatchCreate, AccountSecurityPrecheckRequest, AccountSecurityProfileOverride, AvatarStrategy, ManagedTwoFaRequest, ProfileGenerationStrategy
from app.security import decrypt_secret, encrypt_secret, encrypt_session
from app.storage import save_avatar_bytes
from app.services import accounts as accounts_service
import app.services.account_security.service as account_security_service
from app.services._common import _now
from app.services.account_security import (
    account_security_batch_detail,
    create_account_security_batch,
    drain_account_security_batches,
    precheck_account_security_batch,
    refresh_account_security,
    rotate_managed_two_fa_password,
    save_managed_two_fa_password,
)
from app.services.accounts import create_account
from app.services.accounts import verify_login
from app.services.task_center.service import delete_task, get_task_detail, list_tasks


def _session():
    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return Session(engine)


def _session_factory_no_autoflush():
    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _seed_account(session: Session, *, status: str = AccountStatus.ACTIVE.value, session_value: str = "session") -> TgAccount:
    session.add(Tenant(id=1, name="默认运营空间"))
    app = TelegramDeveloperApp(
        id=1,
        app_name="测试开发者应用",
        api_id=12345,
        api_hash_ciphertext=encrypt_secret("hash"),
        health_status="健康",
    )
    account = TgAccount(
        id=11,
        tenant_id=1,
        display_name="旧账号",
        phone_masked="138****0000",
        developer_app_id=1,
        developer_app_version=1,
        status=status,
        session_ciphertext=encrypt_session(session_value) if session_value else "",
        health_score=90,
    )
    session.add_all([app, account])
    session.commit()
    return account


def _remote_authorization(
    authorization_hash: str,
    *,
    is_current: bool = False,
    device_model: str = "Unknown Device",
    platform: str = "Unknown",
    api_id: int = 999999,
    app_name: str = "Unknown App",
) -> RemoteAuthorizationSnapshot:
    return RemoteAuthorizationSnapshot(
        authorization_hash=authorization_hash,
        is_current=is_current,
        device_model=device_model,
        platform=platform,
        system_version="",
        api_id=api_id,
        app_name=app_name,
        app_version="",
    )


def _remote_cleanup_authorizations() -> list[RemoteAuthorizationSnapshot]:
    return [
        _remote_authorization(
            "primary",
            is_current=True,
            device_model="平台主控",
            platform="Linux",
            app_name="TG运营平台",
        ),
        _remote_authorization("external", device_model="Unknown", platform="Unknown", app_name="Legacy Client"),
        _remote_authorization(
            "official-anchor",
            device_model="Telegram Desktop",
            platform="macOS",
            api_id=2040,
            app_name="Telegram Desktop",
        ),
    ]


def test_sync_remote_profile_cleans_chinese_first_english_last_for_storage(monkeypatch):
    with _session() as session:
        account = _seed_account(session)

        monkeypatch.setattr(accounts_service, "credentials_for_account", lambda _session, _account: None)
        monkeypatch.setattr(
            accounts_service.gateway,
            "pull_profile",
            lambda *_args, **_kwargs: RemoteProfile(first_name="吃瓜群众甲", last_name="Roy", bio="围观中", username="chigua_jia"),
        )

        synced = accounts_service.sync_remote_profile(session, account.id, "tester")

        assert synced.tg_first_name == "吃瓜群众甲"
        assert synced.tg_last_name == ""
        assert synced.display_name == "吃瓜群众甲"
        assert synced.username == "chigua_jia"
        assert "后台展示已清理" in synced.profile_sync_error


def test_refresh_account_security_records_trusted_session_and_external_device():
    with _session() as session:
        account = _seed_account(session)

        snapshot = refresh_account_security(session, 1, account.id, "tester")

        assert snapshot.trusted_session_status == "confirmed"
        assert snapshot.two_fa_status == "missing"
        assert snapshot.external_authorization_count == 0
        assert snapshot.profile_status == "incomplete"


def test_precheck_falls_back_to_local_profile_preview_and_skips_missing_avatar_source():
    with _session() as session:
        account = _seed_account(session)

        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[account.id],
                action_types=["update_profile", "update_username", "update_avatar"],
                profile_strategy=ProfileGenerationStrategy(generation_mode="ai_random", forbidden_words=["违规"]),
            ),
        )

        item = preview.items[0]
        assert preview.summary["executable"] == 1
        assert item.precheck_status == "executable"
        assert item.generated_display_name
        assert item.username_candidates
        assert item.avatar_source == ""
        assert not item.blockers
        assert any("AI 随机命名本次生成失败" in warning for warning in item.warnings)
        assert not any("AI 随机命名暂不可用" in warning for warning in item.warnings)
        assert "未配置头像来源，将跳过头像设置" in item.warnings


def test_ai_random_profile_preview_timeout_warning_is_not_marked_unavailable(monkeypatch):
    with _session() as session:
        account = _seed_account(session)

        def timeout_generation(*args, **kwargs):
            raise TimeoutError("The read operation timed out")

        monkeypatch.setattr(account_security_service, "_generate_profiles_with_ai", timeout_generation)
        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[account.id],
                action_types=["update_profile"],
                profile_strategy=ProfileGenerationStrategy(generation_mode="ai_random"),
            ),
        )

        warnings = preview.items[0].warnings
        assert any("AI 随机命名本次响应超时" in warning for warning in warnings)
        assert not any("不可用" in warning for warning in warnings)


def test_ai_random_profile_preview_uses_healthy_provider_when_tenant_ai_disabled(monkeypatch):
    with _session() as session:
        account = _seed_account(session)
        session.add(
            AiProvider(
                id=1,
                provider_name="测试 AI",
                provider_type="openai_compatible",
                base_url="https://ai.example.test",
                model_name="test-model",
                api_key_ciphertext=encrypt_secret("test-key"),
                health_status="健康",
                is_active=True,
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=False))
        session.commit()

        calls: list[object] = []

        def ai_response(credentials, *_args, **_kwargs):
            calls.append(credentials)
            return json.dumps(
                {
                    "items": [
                        {
                            "display_name": "锅巴洋芋",
                            "first_name": "锅巴洋芋",
                            "last_name": "",
                            "bio": "看到有意思的会回两句",
                            "username_candidates": ["guoba_yangyu", "potato_crisp"],
                        }
                    ]
                },
                ensure_ascii=False,
            ), SimpleNamespace()

        monkeypatch.setattr(account_security_service.ai_gateway, "_post_openai_compatible", ai_response)
        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[account.id],
                action_types=["update_profile", "update_username"],
                profile_strategy=ProfileGenerationStrategy(generation_mode="ai_random"),
            ),
        )

        item = preview.items[0]
        assert len(calls) == 1
        assert item.generated_display_name == "锅巴洋芋"
        assert item.username_candidates == ["guoba_yangyu", "potato_crisp"]
        assert not any("租户 AI 配置未启用" in warning for warning in item.warnings)
        assert not any("AI 随机命名本次生成失败" in warning for warning in item.warnings)


def test_ai_random_profile_preview_requests_large_batch_once(monkeypatch):
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TelegramDeveloperApp(
                id=1,
                app_name="测试开发者应用",
                api_id=12345,
                api_hash_ciphertext=encrypt_secret("hash"),
                health_status="健康",
            )
        )
        provider = AiProvider(
            id=1,
            provider_name="测试 AI",
            provider_type="openai_compatible",
            base_url="https://ai.example.test",
            model_name="test-model",
            api_key_ciphertext=encrypt_secret("test-key"),
            health_status="健康",
            is_active=True,
        )
        session.add(provider)
        session.flush()
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True))
        accounts = [
            TgAccount(
                id=index,
                tenant_id=1,
                display_name=f"账号{index}",
                phone_masked=f"138****{index:04d}",
                developer_app_id=1,
                developer_app_version=1,
                status=AccountStatus.ACTIVE.value,
                session_ciphertext=encrypt_session("session"),
                health_score=90,
            )
            for index in range(1, 51)
        ]
        session.add_all(accounts)
        session.commit()

        calls: list[dict[str, object]] = []

        def batch_ai_response(credentials, prompt, temperature, max_tokens, **kwargs):
            calls.append({"prompt": prompt, "timeout": kwargs.get("timeout"), "max_tokens": max_tokens})
            return json.dumps(
                {
                    "items": [
                        {
                            "display_name": f"测试名{index}",
                            "first_name": f"名{index}",
                            "last_name": "测",
                            "bio": "批量生成资料",
                            "username_candidates": [f"testuser_{index:03d}"],
                        }
                        for index in range(1, 51)
                    ]
                },
                ensure_ascii=False,
            ), SimpleNamespace()

        monkeypatch.setattr(account_security_service.ai_gateway, "_post_openai_compatible", batch_ai_response)
        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[account.id for account in accounts],
                action_types=["update_profile", "update_username"],
                profile_strategy=ProfileGenerationStrategy(generation_mode="ai_random", custom_prompt="像锅巴洋芋、蕉太狼这种随机网名"),
            ),
        )

        assert len(calls) == 1
        assert "一次性生成 50 组随机账号资料" in str(calls[0]["prompt"])
        assert "像锅巴洋芋、蕉太狼这种随机网名" in str(calls[0]["prompt"])
        assert calls[0]["timeout"] == 180
        assert preview.summary["total"] == 50
        assert preview.summary["executable"] == 50
        assert preview.items[0].generated_display_name == "测试名1"
        assert preview.items[-1].generated_display_name == "测试名50"


def test_local_profile_preview_diversifies_large_batches():
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TelegramDeveloperApp(
                id=1,
                app_name="测试开发者应用",
                api_id=12345,
                api_hash_ciphertext=encrypt_secret("hash"),
                health_status="健康",
            )
        )
        accounts = [
            TgAccount(
                id=index,
                tenant_id=1,
                display_name=f"导入0524-8740-{index:03d}",
                phone_masked=f"138****{index:04d}",
                developer_app_id=1,
                developer_app_version=1,
                status=AccountStatus.ACTIVE.value,
                session_ciphertext=encrypt_session("session"),
                health_score=90,
            )
            for index in range(1, 101)
        ]
        session.add_all(accounts)
        session.commit()

        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[account.id for account in accounts],
                action_types=["update_profile", "update_username"],
                profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
            ),
        )

        names = [item.generated_display_name for item in preview.items]
        bios = [item.generated_bio for item in preview.items]
        username_bases = {item.username_candidates[0].rsplit("_", 1)[0] for item in preview.items}
        assert len(set(names)) == 100
        assert len({len(bio) for bio in bios}) >= 8
        assert len(username_bases) >= 30


def test_precheck_invalid_avatar_source_warns_and_keeps_batch_executable():
    with _session() as session:
        account = _seed_account(session)

        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[account.id],
                action_types=["update_profile", "update_avatar"],
                profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
                avatar_strategy=AvatarStrategy(mode="sequential", avatar_sources=["material:999"]),
            ),
        )

        item = preview.items[0]
        assert item.precheck_status == "executable"
        assert item.avatar_source == ""
        assert "头像素材不存在或不属于当前租户" in item.warnings


def test_material_random_avatar_strategy_picks_reviewed_uploaded_image(tmp_path):
    with _session() as session:
        account = _seed_account(session)
        avatar_path = tmp_path / "avatar.png"
        avatar_path.write_bytes(b"\x89PNG\r\n\x1a\navatar")
        session.add(
            Material(
                id=701,
                tenant_id=1,
                title="头像包A-avatar",
                material_type="图片",
                content=str(avatar_path),
                tags="头像",
                review_status="已审核",
                source_kind="upload",
                mime_type="image/png",
                file_size=avatar_path.stat().st_size,
            )
        )
        session.commit()

        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[account.id],
                action_types=["update_avatar"],
                profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
                avatar_strategy=AvatarStrategy(mode="material_random"),
            ),
        )

        item = preview.items[0]
        assert item.precheck_status == "executable"
        assert item.avatar_source == "material:701"
        assert not item.warnings


def test_prd_random_from_material_pool_avatar_strategy_picks_reviewed_uploaded_image(tmp_path):
    with _session() as session:
        account = _seed_account(session)
        avatar_path = tmp_path / "prd-avatar.png"
        avatar_path.write_bytes(b"\x89PNG\r\n\x1a\navatar")
        session.add(
            Material(
                id=702,
                tenant_id=1,
                title="资料初始化头像包",
                material_type="图片",
                content=str(avatar_path),
                tags="头像",
                review_status="已审核",
                source_kind="upload",
                mime_type="image/png",
                file_size=avatar_path.stat().st_size,
            )
        )
        session.commit()

        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[account.id],
                action_types=["update_avatar"],
                profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
                avatar_strategy=AvatarStrategy(mode="random_from_material_pool"),
            ),
        )

        item = preview.items[0]
        assert item.precheck_status == "executable"
        assert item.avatar_source == "material:702"
        assert not item.warnings


def test_random_avatar_strategy_uses_ready_cached_uploaded_image_without_local_file():
    with _session() as session:
        account = _seed_account(session)
        session.add(
            Material(
                id=706,
                tenant_id=1,
                title="资料初始化头像包-已缓存",
                material_type="图片",
                content="",
                tags="头像",
                review_status="已审核",
                source_kind="upload",
                mime_type="image/png",
                file_size=123,
                cache_ready_status="ready",
                tg_cache_account_id=account.id,
                tg_cache_peer_id="@avatar_cache",
                tg_cache_message_id="88",
            )
        )
        session.commit()

        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[account.id],
                action_types=["update_avatar"],
                profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
                avatar_strategy=AvatarStrategy(mode="random_from_material_pool"),
            ),
        )

        item = preview.items[0]
        assert item.precheck_status == "executable"
        assert item.avatar_source == "material:706"
        assert not item.warnings


def test_created_batch_stays_running_with_no_autoflush_session():
    session_factory = _session_factory_no_autoflush()
    with session_factory() as session:
        account = _seed_account(session)
        batch = create_account_security_batch(
            session,
            1,
            AccountSecurityBatchCreate(
                account_ids=[account.id],
                action_types=["update_profile"],
                confirm_text="确认",
                profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
                reason="测试无自动刷新 session 创建批次",
            ),
            "tester",
        )

        assert batch.status == "running"
        assert batch.items[0].status == "pending"

    assert drain_account_security_batches(session_factory, limit=10) == 1


def test_profile_batch_is_visible_as_readonly_task_center_projection():
    with _session() as session:
        account = _seed_account(session)
        payload = AccountSecurityBatchCreate(
            account_ids=[account.id],
            action_types=["update_profile"],
            confirm_text="确认",
            profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
            reason="测试任务中心投影",
        )
        batch = create_account_security_batch(session, 1, payload, "tester")

        rows = list_tasks(session, 1, task_type="account_profile_init")
        assert [row["id"] for row in rows] == [f"account_security_batch:{batch.id}"]
        assert rows[0]["type"] == "account_profile_init"
        assert rows[0]["name"] == f"资料初始化批次 #{batch.id}"
        assert rows[0]["stats"]["batch_status"] == "running"
        assert rows[0]["stats"]["pending_count"] == 1
        assert rows[0]["target_summary"] == "账号资料初始化 / 1 个账号"

        detail = get_task_detail(session, 1, f"account_security_batch:{batch.id}")

        assert detail["actions"] == []
        assert detail["profile_batch"]["batch_id"] == batch.id
        assert detail["account_security_batch"]["system_task_type"] == "account_profile_init"
        assert detail["profile_batch"]["items"][0]["account_id"] == account.id
        assert detail["profile_batch"]["items"][0]["profile_status"] == "pending"


def test_profile_batch_task_list_uses_lightweight_projection(monkeypatch):
    from app.services.task_center import profile_batch_projection

    with _session() as session:
        account = _seed_account(session)
        payload = AccountSecurityBatchCreate(
            account_ids=[account.id],
            action_types=["update_profile"],
            confirm_text="确认",
            profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
            reason="测试列表轻量投影",
        )
        batch = create_account_security_batch(session, 1, payload, "tester")

        def fail_detail_search(*_args, **_kwargs):
            raise AssertionError("task list must not build profile-batch detail search text")

        monkeypatch.setattr(profile_batch_projection, "_projection_search_text", fail_detail_search)
        rows = list_tasks(session, 1, task_type="account_profile_init")

    assert [row["id"] for row in rows] == [f"account_security_batch:{batch.id}"]


def test_account_security_system_task_cannot_be_deleted_from_task_center():
    with _session() as session:
        account = _seed_account(session)
        payload = AccountSecurityBatchCreate(
            account_ids=[account.id],
            action_types=["update_profile"],
            confirm_text="确认",
            profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
            reason="测试删除投影任务",
        )
        batch = create_account_security_batch(session, 1, payload, "tester")

        with pytest.raises(PermissionError, match="cannot be deleted"):
            delete_task(session, 1, f"account_security_batch:{batch.id}", "tester", "用户删除")

        assert list_tasks(session, 1, task_type="account_profile_init")
        detail = get_task_detail(session, 1, f"account_security_batch:{batch.id}")

        db_batch = session.get(TgAccountSecurityBatch, batch.id)
        db_item = session.scalar(select(TgAccountSecurityBatchItem).where(TgAccountSecurityBatchItem.batch_id == batch.id))
        assert detail["account_security_batch"]["batch_id"] == batch.id
        assert db_batch.status == "running"
        assert db_batch.finished_at is None
        assert db_item.status == "pending"
        assert db_item.skipped_reason == ""


def test_account_security_batches_project_cleanup_2fa_and_standby_task_types():
    with _session() as session:
        account = _seed_account(session)
        session.add(AccountProxy(id=1, tenant_id=1, name="备用代理", port=1080, status="healthy", alert_status="normal"))
        session.commit()
        batches = [
            create_account_security_batch(
                session,
                1,
                AccountSecurityBatchCreate(account_ids=[account.id], action_types=[action], confirm_text="确认", reason=reason),
                "tester",
            )
            for action, reason in [
                ("cleanup_devices", "清理登录设备"),
                ("set_two_fa", "设置二步密码"),
                ("provision_standby_session", "补齐备用 session"),
            ]
        ]

        assert [row["type"] for row in list_tasks(session, 1, task_type="account_device_cleanup")] == ["account_device_cleanup"]
        assert [row["type"] for row in list_tasks(session, 1, task_type="account_2fa_setup")] == ["account_2fa_setup"]
        standby_rows = list_tasks(session, 1, task_type="account_standby_session_provision")
        assert [row["type"] for row in standby_rows] == ["account_standby_session_provision"]
        assert standby_rows[0]["target_summary"] == "备用 session 补齐 / 1 个账号"

        detail = get_task_detail(session, 1, f"account_security_batch:{batches[-1].id}")
        item = detail["account_security_batch"]["items"][0]
        assert detail["account_security_batch"]["system_task_type"] == "account_standby_session_provision"
        assert item["standby_session_status"] == "pending"
        assert item["preserved_devices_summary"] == "primary / standby_1 / standby_2 / 官方锚点设备"


def test_standby_slot_strategy_is_accepted_by_security_payload_schema():
    payload = AccountSecurityBatchCreate(
        account_ids=[11],
        action_types=["provision_standby_session"],
        standby_slot_strategy="standby_2",
        confirm_text="确认",
        reason="补齐备用授权",
    )

    assert payload.standby_slot_strategy == "standby_2"


def test_standby_session_precheck_blocks_missing_auto_login_resources():
    with _session() as session:
        account = _seed_account(session)

        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[account.id],
                action_types=["provision_standby_session"],
            ),
        )
        item = preview.items[0]

        assert preview.summary["manual_required"] == 1
        assert item.precheck_status == "manual_required"
        assert "没有可用代理用于备用 session 登录" in item.blockers
        assert "账号未托管 2FA" in ";".join(item.warnings)


def test_standby_session_batch_exposes_manual_required_instead_of_fake_success(monkeypatch):
    with _session() as session:
        account = _seed_account(session)
        session.add(AccountProxy(id=1, tenant_id=1, name="备用代理", port=1080, status="healthy", alert_status="normal"))
        monkeypatch.setattr(
            account_security_service.gateway,
            "start_login",
            lambda *_args, **_kwargs: SimpleNamespace(status="等待验证码", code_preview=None, code_expires_at=_now(), qr_payload=None),
        )
        monkeypatch.setattr(account_security_service.gateway, "poll_verification_codes", lambda *_args, **_kwargs: [])
        monkeypatch.setattr(account_security_service.time, "sleep", lambda *_args, **_kwargs: None)
        batch = create_account_security_batch(
            session,
            1,
            AccountSecurityBatchCreate(
                account_ids=[account.id],
                action_types=["provision_standby_session"],
                confirm_text="确认",
                reason="补齐备用 session",
            ),
            "tester",
        )

        assert drain_account_security_batches(lambda: Session(session.bind), limit=10) == 1
        detail = get_task_detail(session, 1, f"account_security_batch:{batch.id}")
        item = detail["account_security_batch"]["items"][0]

        assert detail["account_security_batch"]["batch_status"] == "manual_required"
        assert detail["task"]["status"] == "stopped"
        assert detail["task"]["stats"]["success_count"] == 0
        assert item["status"] == "manual_required"
        assert item["standby_session_status"] == "code_waiting"
        assert item["failure_type"] == "verification_code_unreadable"
        assert "验证码不可读取" in item["failure_detail"]
        assert item["target_slot"] == "standby_1"
        assert item["developer_app_label"] == "测试开发者应用"
        assert item["proxy_label"] == "备用代理"
        assert item["verification_code_status"] == "验证码不可读取"


def test_standby_session_batch_requires_managed_2fa_when_telegram_requests_password(monkeypatch):
    with _session() as session:
        account = _seed_account(session)
        session.add(AccountProxy(id=1, tenant_id=1, name="备用代理", port=1080, status="healthy", alert_status="normal"))
        monkeypatch.setattr(
            account_security_service.gateway,
            "start_login",
            lambda *_args, **_kwargs: SimpleNamespace(status="等待验证码", code_preview="12345", code_expires_at=_now() + timedelta(minutes=3), qr_payload=None),
        )
        monkeypatch.setattr(
            account_security_service.gateway,
            "finish_login",
            lambda *_args, **_kwargs: (AccountStatus.WAITING_2FA.value, ""),
        )
        batch = create_account_security_batch(
            session,
            1,
            AccountSecurityBatchCreate(
                account_ids=[account.id],
                action_types=["provision_standby_session"],
                confirm_text="确认",
                reason="补齐备用 session",
            ),
            "tester",
        )

        assert drain_account_security_batches(lambda: Session(session.bind), limit=10) == 1
        detail = get_task_detail(session, 1, f"account_security_batch:{batch.id}")
        item = detail["account_security_batch"]["items"][0]

        assert detail["account_security_batch"]["batch_status"] == "manual_required"
        assert item["status"] == "manual_required"
        assert item["standby_session_status"] == "two_fa_waiting"
        assert item["failure_type"] == "two_fa_not_managed"
        assert "未托管 2FA" in item["failure_detail"]
        assert item["target_slot"] == "standby_1"
        assert item["verification_code_status"] == "已读取"
        assert item["two_fa_usage_status"] == "未托管 2FA"


def test_standby_session_batch_auto_provisions_missing_standby_slot(monkeypatch):
    with _session() as session:
        account = _seed_account(session)
        session.add(AccountProxy(id=1, tenant_id=1, name="备用代理", port=1080, status="healthy", alert_status="normal"))
        save_managed_two_fa_password(
            session,
            1,
            account.id,
            ManagedTwoFaRequest(password="managed-password", reason="首次托管"),
            "tester",
        )
        monkeypatch.setattr(
            account_security_service.gateway,
            "start_login",
            lambda *_args, **_kwargs: SimpleNamespace(status="等待验证码", code_preview="12345", code_expires_at=_now() + timedelta(minutes=3), qr_payload=None),
        )
        monkeypatch.setattr(
            account_security_service.gateway,
            "finish_login",
            lambda *_args, **_kwargs: (AccountStatus.ACTIVE.value, "standby-session-raw"),
        )
        batch = create_account_security_batch(
            session,
            1,
            AccountSecurityBatchCreate(
                account_ids=[account.id],
                action_types=["provision_standby_session"],
                standby_slot_strategy="standby_2",
                confirm_text="确认",
                reason="自动补齐备用 session",
            ),
            "tester",
        )

        assert drain_account_security_batches(lambda: Session(session.bind), limit=10) == 1
        refreshed = account_security_batch_detail(session, 1, batch.id)
        asset = session.scalar(select(TgAccountAuthorization).where(TgAccountAuthorization.account_id == account.id, TgAccountAuthorization.role == "standby_2"))

        assert refreshed.status == "succeeded"
        assert refreshed.items[0].status == "succeeded"
        assert asset is not None
        assert asset.session_ciphertext
        detail = get_task_detail(session, 1, f"account_security_batch:{batch.id}")
        item = detail["account_security_batch"]["items"][0]
        assert item["target_slot"] == "standby_2"
        assert item["developer_app_label"] == "测试开发者应用"
        assert item["proxy_label"] == "备用代理"
        assert item["verification_code_status"] == "已读取"
        assert item["two_fa_usage_status"] == "已使用托管 2FA"


def test_standby_session_batch_polls_primary_session_code_when_challenge_has_no_preview(monkeypatch):
    with _session() as session:
        account = _seed_account(session)
        session.add(AccountProxy(id=1, tenant_id=1, name="备用代理", port=1080, status="healthy", alert_status="normal"))
        save_managed_two_fa_password(
            session,
            1,
            account.id,
            ManagedTwoFaRequest(password="managed-password", reason="首次托管"),
            "tester",
        )
        monkeypatch.setattr(
            account_security_service.gateway,
            "start_login",
            lambda *_args, **_kwargs: SimpleNamespace(status="等待验证码", code_preview=None, code_expires_at=_now() + timedelta(minutes=3), qr_payload=None),
        )
        poll_attempts = {"count": 0}

        def poll_verification_codes(*_args, **_kwargs):
            poll_attempts["count"] += 1
            if poll_attempts["count"] == 1:
                return []
            return [SimpleNamespace(code="67890", raw_hint="TG 官方服务消息验证码", expires_at=_now() + timedelta(minutes=3))]

        monkeypatch.setattr(
            account_security_service.gateway,
            "poll_verification_codes",
            poll_verification_codes,
        )
        monkeypatch.setattr(account_security_service.time, "sleep", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            account_security_service.gateway,
            "finish_login",
            lambda code, *_args, **_kwargs: (AccountStatus.ACTIVE.value, f"standby-session-{code}"),
        )
        batch = create_account_security_batch(
            session,
            1,
            AccountSecurityBatchCreate(
                account_ids=[account.id],
                action_types=["provision_standby_session"],
                standby_slot_strategy="standby_1",
                confirm_text="确认",
                reason="自动补齐备用 session",
            ),
            "tester",
        )

        assert drain_account_security_batches(lambda: Session(session.bind), limit=10) == 1
        refreshed = account_security_batch_detail(session, 1, batch.id)
        asset = session.scalar(select(TgAccountAuthorization).where(TgAccountAuthorization.account_id == account.id, TgAccountAuthorization.role == "standby_1"))
        code = session.scalar(select(TgVerificationCode).where(TgVerificationCode.account_id == account.id, TgVerificationCode.source == "standby_authorization_auto_login"))

        assert refreshed.status == "succeeded"
        assert refreshed.items[0].status == "succeeded"
        assert asset is not None
        assert asset.session_ciphertext
        assert code is not None
        assert code.code_preview == "67890"
        assert poll_attempts["count"] == 2


def test_standby_session_batch_auto_provisions_both_missing_slots_without_manual_codes(monkeypatch):
    with _session() as session:
        account = _seed_account(session)
        session.add(AccountProxy(id=1, tenant_id=1, name="备用代理", port=1080, status="healthy", alert_status="normal"))
        save_managed_two_fa_password(
            session,
            1,
            account.id,
            ManagedTwoFaRequest(password="managed-password", reason="首次托管"),
            "tester",
        )
        started_roles: list[str] = []
        submitted_codes: list[str] = []
        submitted_passwords: list[str | None] = []
        rotations: list[dict[str, str | None]] = []
        poll_codes = iter(["11111", "22222"])

        def start_login(*_args, **kwargs):
            started_roles.append(kwargs.get("credentials").app_name if kwargs.get("credentials") else "")
            return SimpleNamespace(status="等待验证码", code_preview=None, code_expires_at=_now() + timedelta(minutes=3), qr_payload=None)

        def poll_verification_codes(*_args, **_kwargs):
            code = next(poll_codes)
            return [SimpleNamespace(code=code, raw_hint="TG 官方服务消息验证码", expires_at=_now() + timedelta(minutes=3))]

        def finish_login(code, password_2fa, *_args, **_kwargs):
            submitted_codes.append(code)
            submitted_passwords.append(password_2fa)
            return AccountStatus.ACTIVE.value, f"standby-session-{code}"

        def set_two_fa(session_ciphertext, password, **kwargs):
            rotations.append(
                {
                    "session_ciphertext": session_ciphertext,
                    "password": password,
                    "current_password": kwargs.get("current_password"),
                }
            )
            return SimpleNamespace(ok=True, status="enabled", detail="", failure_type="")

        monkeypatch.setattr(account_security_service.gateway, "start_login", start_login)
        monkeypatch.setattr(account_security_service.gateway, "poll_verification_codes", poll_verification_codes)
        monkeypatch.setattr(account_security_service.gateway, "finish_login", finish_login)
        monkeypatch.setattr(account_security_service.gateway, "set_two_fa_password", set_two_fa)
        batch = create_account_security_batch(
            session,
            1,
            AccountSecurityBatchCreate(
                account_ids=[account.id],
                action_types=["provision_standby_session"],
                standby_slot_strategy="auto_missing",
                confirm_text="确认",
                reason="自动补齐两个备用 session",
            ),
            "tester",
        )

        assert drain_account_security_batches(lambda: Session(session.bind), limit=10) == 1
        refreshed = account_security_batch_detail(session, 1, batch.id)
        roles = set(
            session.scalars(
                select(TgAccountAuthorization.role).where(
                    TgAccountAuthorization.account_id == account.id,
                    TgAccountAuthorization.role.in_(["standby_1", "standby_2"]),
                )
            )
        )
        codes = list(
            session.scalars(
                select(TgVerificationCode.code_preview)
                .where(TgVerificationCode.account_id == account.id, TgVerificationCode.source == "standby_authorization_auto_login")
                .order_by(TgVerificationCode.id.asc())
            )
        )

        assert refreshed.status == "succeeded"
        assert refreshed.items[0].status == "succeeded"
        assert roles == {"standby_1", "standby_2"}
        assert submitted_codes == ["11111", "22222"]
        assert submitted_passwords == ["managed-password", rotations[0]["password"]]
        assert [rotation["current_password"] for rotation in rotations] == ["managed-password", submitted_passwords[1]]
        assert codes == ["11111", "22222"]
        snapshot = session.scalar(select(TgAccountSecuritySnapshot).where(TgAccountSecuritySnapshot.account_id == account.id))
        assert snapshot is not None
        assert decrypt_secret(snapshot.two_fa_password_ciphertext) == rotations[-1]["password"]
        detail = get_task_detail(session, 1, f"account_security_batch:{batch.id}")
        item = detail["account_security_batch"]["items"][0]
        assert item["target_slot"] == "standby_1 / standby_2"


def test_primary_login_with_2fa_rotates_and_saves_new_managed_password(monkeypatch):
    with _session() as session:
        account = _seed_account(session, status=AccountStatus.WAITING_2FA.value, session_value="")
        rotations: list[dict[str, str | None]] = []

        def finish_login(code, password_2fa, *_args, **_kwargs):
            assert code is None
            assert password_2fa == "old-2fa-password"
            return AccountStatus.ACTIVE.value, "primary-session-raw"

        def set_two_fa(session_ciphertext, password, **kwargs):
            rotations.append(
                {
                    "session_ciphertext": session_ciphertext,
                    "password": password,
                    "current_password": kwargs.get("current_password"),
                }
            )
            return SimpleNamespace(ok=True, status="enabled", detail="", failure_type="")

        monkeypatch.setattr(accounts_service.gateway, "finish_login", finish_login)
        monkeypatch.setattr(accounts_service.gateway, "set_two_fa_password", set_two_fa)

        verified = verify_login(session, account.id, None, "old-2fa-password", actor="tester")
        snapshot = session.scalar(select(TgAccountSecuritySnapshot).where(TgAccountSecuritySnapshot.account_id == account.id))

        assert verified.status == AccountStatus.ACTIVE.value
        assert rotations
        assert rotations[0]["current_password"] == "old-2fa-password"
        assert rotations[0]["password"] != "old-2fa-password"
        assert snapshot is not None
        assert snapshot.two_fa_status == "enabled"
        assert decrypt_secret(snapshot.two_fa_password_ciphertext) == rotations[0]["password"]


def test_standby_session_self_heal_activates_existing_standby_when_primary_session_missing():
    with _session() as session:
        account = _seed_account(session, status=AccountStatus.WAITING_CODE.value, session_value="")
        session.add(
            TgAccountAuthorization(
                tenant_id=1,
                account_id=account.id,
                role="standby_1",
                developer_app_id=1,
                session_ciphertext=encrypt_session("standby-session"),
                status="standby",
                health_status="healthy",
            )
        )
        session.commit()
        batch = create_account_security_batch(
            session,
            1,
            AccountSecurityBatchCreate(
                account_ids=[account.id],
                action_types=["self_heal_session"],
                confirm_text="确认",
                reason="用备用 session 恢复",
            ),
            "tester",
        )

        assert batch.items[0].status == "pending"
        assert drain_account_security_batches(lambda: Session(session.bind), limit=10) == 1
        refreshed = account_security_batch_detail(session, 1, batch.id)
        updated = session.get(TgAccount, account.id)

        assert refreshed.status == "succeeded"
        assert refreshed.items[0].status == "succeeded"
        assert updated.status == AccountStatus.ACTIVE.value
        assert updated.session_ciphertext


def test_profile_batch_avatar_waits_until_material_cache_ready(tmp_path, monkeypatch):
    with _session() as session:
        account = _seed_account(session)
        avatar_path = tmp_path / "waiting-avatar.png"
        avatar_path.write_bytes(b"\x89PNG\r\n\x1a\navatar")
        session.add(
            Material(
                id=703,
                tenant_id=1,
                title="未缓存头像",
                material_type="图片",
                content=str(avatar_path),
                tags="头像",
                review_status="已审核",
                source_kind="upload",
                mime_type="image/png",
                file_size=avatar_path.stat().st_size,
                cache_ready_status="not_cached",
            )
        )
        session.commit()
        payload = AccountSecurityBatchCreate(
            account_ids=[account.id],
            action_types=["update_avatar"],
            confirm_text="确认",
            profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
            avatar_strategy=AvatarStrategy(mode="sequential", avatar_sources=["material:703"]),
            reason="测试等待头像缓存",
        )
        batch = create_account_security_batch(session, 1, payload, "tester")

        def fail_avatar_upload(*args, **kwargs):
            raise AssertionError("avatar update must wait for TG material cache")

        monkeypatch.setattr(account_security_service.gateway, "update_profile", fail_avatar_upload)

        assert drain_account_security_batches(lambda: Session(session.bind), limit=10) == 1
        refreshed = account_security_batch_detail(session, 1, batch.id)
        item = refreshed.items[0]
        assert refreshed.status == "running"
        assert item.status == "waiting"
        assert item.avatar_status == "waiting_cache"
        assert item.failure_type == "waiting_material_cache"

        detail = get_task_detail(session, 1, f"account_security_batch:{batch.id}")
        projected_item = detail["profile_batch"]["items"][0]
        assert detail["profile_batch"]["avatar_cache"]["waiting"] == 1
        assert projected_item["avatar_cache_status"] == "not_cached"
        assert projected_item["avatar_preview_url"] == ""


def test_profile_batch_keeps_running_when_profile_succeeds_but_avatar_waits(tmp_path, monkeypatch):
    with _session() as session:
        account = _seed_account(session)
        avatar_path = tmp_path / "profile-plus-waiting-avatar.png"
        avatar_path.write_bytes(b"\x89PNG\r\n\x1a\navatar")
        session.add(
            Material(
                id=705,
                tenant_id=1,
                title="未缓存头像",
                material_type="图片",
                content=str(avatar_path),
                tags="头像",
                review_status="已审核",
                source_kind="upload",
                mime_type="image/png",
                file_size=avatar_path.stat().st_size,
                cache_ready_status="not_cached",
            )
        )
        session.commit()
        payload = AccountSecurityBatchCreate(
            account_ids=[account.id],
            action_types=["update_profile", "update_avatar"],
            confirm_text="确认",
            profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
            avatar_strategy=AvatarStrategy(mode="sequential", avatar_sources=["material:705"]),
            reason="资料成功但头像等待缓存",
        )
        batch = create_account_security_batch(session, 1, payload, "tester")

        def fake_update_profile(*args, **kwargs):
            if kwargs.get("avatar_path"):
                raise AssertionError("avatar update must wait for TG material cache")
            return SimpleNamespace(ok=True, detail="profile ok", failure_type="")

        monkeypatch.setattr(account_security_service.gateway, "update_profile", fake_update_profile)

        assert drain_account_security_batches(lambda: Session(session.bind), limit=10) == 1
        refreshed = account_security_batch_detail(session, 1, batch.id)
        item = refreshed.items[0]

        assert refreshed.status == "running"
        assert item.status == "waiting"
        assert item.profile_status == "succeeded"
        assert item.avatar_status == "waiting_cache"
        assert item.failure_type == "waiting_material_cache"

        db_material = session.get(Material, 705)
        db_item = session.get(TgAccountSecurityBatchItem, item.id)
        db_material.cache_ready_status = "ready"
        db_material.tg_cache_peer_id = "@avatar_cache"
        db_material.tg_cache_message_id = "89"
        db_material.tg_cache_account_id = account.id
        db_item.next_retry_at = _now() - timedelta(seconds=1)
        session.commit()

        def fail_profile_repeat(*args, **kwargs):
            if not kwargs.get("avatar_path"):
                raise AssertionError("profile step already succeeded and must not be repeated")
            return SimpleNamespace(ok=True, detail="avatar ok", failure_type="")

        monkeypatch.setattr(account_security_service.gateway, "update_profile", fail_profile_repeat)

        assert drain_account_security_batches(lambda: Session(session.bind), limit=10) == 1
        retried = account_security_batch_detail(session, 1, batch.id)
        retried_item = retried.items[0]
        assert retried.status == "succeeded"
        assert retried_item.profile_status == "succeeded"
        assert retried_item.avatar_status == "succeeded"


def test_profile_batch_avatar_uses_ready_material_cache_when_source_file_was_temp(monkeypatch):
    with _session() as session:
        account = _seed_account(session)
        session.add(
            Material(
                id=704,
                tenant_id=1,
                title="已暂存头像",
                material_type="图片",
                content="",
                tags="头像",
                review_status="已审核",
                source_kind="upload",
                mime_type="image/png",
                cache_ready_status="ready",
                tg_cache_peer_id="@avatar_cache",
                tg_cache_message_id="88",
                tg_cache_account_id=account.id,
            )
        )
        session.commit()
        payload = AccountSecurityBatchCreate(
            account_ids=[account.id],
            action_types=["update_avatar"],
            confirm_text="确认",
            profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
            avatar_strategy=AvatarStrategy(mode="sequential", avatar_sources=["material:704"]),
            reason="测试已暂存头像回显",
        )
        batch = create_account_security_batch(session, 1, payload, "tester")

        def fake_download(*args, **kwargs):
            return SimpleNamespace(ok=True, data=b"\x89PNG\r\n\x1a\ncached-avatar", failure_type="", detail="")

        def fake_update_profile(*args, **kwargs):
            assert kwargs["avatar_path"]
            assert os.path.exists(kwargs["avatar_path"])
            return SimpleNamespace(ok=True, detail="ok", failure_type="")

        monkeypatch.setattr(account_security_service.gateway, "download_cached_material", fake_download)
        monkeypatch.setattr(account_security_service.gateway, "update_profile", fake_update_profile)

        assert drain_account_security_batches(lambda: Session(session.bind), limit=10) == 1
        refreshed = account_security_batch_detail(session, 1, batch.id)
        item = refreshed.items[0]
        projected_item = get_task_detail(session, 1, f"account_security_batch:{batch.id}")["profile_batch"]["items"][0]

        assert refreshed.status == "succeeded"
        assert item.avatar_status == "succeeded"
        assert session.get(TgAccount, account.id).avatar_object_key.startswith(f"avatars/1/{account.id}/")
        assert projected_item["avatar_cache_status"] == "ready"
        assert projected_item["avatar_preview_url"].startswith("/media/avatars/1/")


def test_profile_preview_does_not_refresh_security_state(monkeypatch):
    with _session() as session:
        account = _seed_account(session)

        def fail_refresh(*args, **kwargs):
            raise AssertionError("profile preview should not scan live security state")

        monkeypatch.setattr(account_security_service, "refresh_account_security", fail_refresh)
        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[account.id],
                action_types=["update_profile", "update_username", "update_avatar"],
                profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
            ),
        )

        assert preview.items[0].precheck_status == "executable"


def test_security_only_precheck_does_not_require_ai_profile_generation():
    with _session() as session:
        account = _seed_account(session)

        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[account.id],
                action_types=["cleanup_devices", "set_two_fa"],
                profile_strategy=ProfileGenerationStrategy(generation_mode="ai_random"),
            ),
        )

        item = preview.items[0]
        assert preview.summary["executable"] == 1
        assert item.precheck_status == "executable"
        assert not any("AI 随机命名" in blocker for blocker in item.blockers)
        assert item.generated_display_name == account.display_name


def test_unknown_account_security_action_is_rejected():
    with _session() as session:
        account = _seed_account(session)

        with pytest.raises(ValueError, match="unsupported account security actions"):
            precheck_account_security_batch(
                session,
                1,
                AccountSecurityPrecheckRequest(account_ids=[account.id], action_types=["set_trusted_device_label"]),
            )


def test_confirmed_batch_drains_profile_username_and_device_cleanup_independently():
    with _session() as session:
        account = _seed_account(session)
        avatar_object_key, _avatar_path = save_avatar_bytes(tenant_id=account.tenant_id, account_id=account.id, content_type="image/png", data=b"avatar")
        payload = AccountSecurityBatchCreate(
            account_ids=[account.id],
            action_types=["cleanup_devices", "set_two_fa", "update_profile", "update_username", "update_avatar"],
            confirm_text="确认加固",
            profile_strategy=ProfileGenerationStrategy(generation_mode="template", username_prefix_hint="acct"),
            avatar_strategy=AvatarStrategy(mode="sequential", avatar_sources=[f"avatar:{avatar_object_key}"]),
            reason="测试批量加固",
        )
        batch = create_account_security_batch(session, 1, payload, "tester")

        assert batch.status == "running"
        processed = drain_account_security_batches(lambda: Session(session.bind), limit=10)
        refreshed = account_security_batch_detail(session, 1, batch.id)

        assert processed == 1
        assert refreshed.status == "succeeded"
        assert refreshed.items[0].cleanup_status == "succeeded"
        assert refreshed.items[0].two_fa_status == "enabled"
        assert refreshed.items[0].profile_status == "succeeded"
        assert refreshed.items[0].username_status == "succeeded"
        assert refreshed.items[0].avatar_status == "succeeded"
        assert session.get(TgAccount, account.id).username.startswith("acct_")
        assert session.get(TgAccount, account.id).avatar_object_key == avatar_object_key
        snapshot = session.scalar(select(TgAccountSecuritySnapshot).where(TgAccountSecuritySnapshot.account_id == account.id))
        assert snapshot.external_authorization_count == 0
        assert snapshot.two_fa_password_ciphertext


def test_device_cleanup_preserves_current_and_platform_trusted_authorizations(monkeypatch):
    with _session() as session:
        account = _seed_account(session)
        cleaned_hashes: list[str] = []

        def record_cleanup(_session_ciphertext, authorization_hash, _credentials):
            cleaned_hashes.append(authorization_hash)
            return SimpleNamespace(ok=True, detail="cleaned", failure_type="")

        monkeypatch.setattr(account_security_service.gateway, "list_authorizations", lambda *_args, **_kwargs: _remote_cleanup_authorizations())
        monkeypatch.setattr(account_security_service.gateway, "cleanup_authorization", record_cleanup)
        batch = create_account_security_batch(
            session,
            1,
            AccountSecurityBatchCreate(account_ids=[account.id], action_types=["cleanup_devices"], confirm_text="确认"),
            "tester",
        )

        assert drain_account_security_batches(lambda: Session(session.bind), limit=10) == 1
        refreshed = account_security_batch_detail(session, 1, batch.id)

        assert cleaned_hashes == ["external"]
        assert refreshed.items[0].external_devices_before == 1
        assert refreshed.items[0].external_devices_after == 0


def test_device_cleanup_preserves_recorded_primary_and_standby_authorization_hashes(monkeypatch):
    with _session() as session:
        account = _seed_account(session)
        session.add_all(
            [
                TgAccountAuthorization(
                    tenant_id=1,
                    account_id=account.id,
                    role="standby_1",
                    session_ciphertext=encrypt_session("standby-session"),
                    status="standby",
                    telegram_authorization_hash_ciphertext=encrypt_secret("standby-hash"),
                ),
            ]
        )
        session.commit()
        cleaned_hashes: list[str] = []

        def record_cleanup(_session_ciphertext, authorization_hash, _credentials):
            cleaned_hashes.append(authorization_hash)
            return SimpleNamespace(ok=True, detail="cleaned", failure_type="")

        monkeypatch.setattr(
            account_security_service.gateway,
            "list_authorizations",
            lambda *_args, **_kwargs: [
                _remote_authorization("primary", is_current=True, device_model="平台主控", platform="Linux", app_name="TG运营平台"),
                _remote_authorization("standby-hash", device_model="Standby", platform="Linux", app_name="TG运营平台备用"),
                _remote_authorization("external-hash", device_model="Unknown", platform="Unknown", app_name="Legacy Client"),
                _remote_authorization("official-anchor", device_model="Telegram Desktop", platform="macOS", api_id=2040, app_name="Telegram Desktop"),
            ],
        )
        monkeypatch.setattr(account_security_service.gateway, "cleanup_authorization", record_cleanup)
        batch = create_account_security_batch(
            session,
            1,
            AccountSecurityBatchCreate(account_ids=[account.id], action_types=["cleanup_devices"], confirm_text="确认"),
            "tester",
        )

        assert drain_account_security_batches(lambda: Session(session.bind), limit=10) == 1

        assert cleaned_hashes == ["external-hash"]


def test_device_cleanup_requires_official_anchor_authorization(monkeypatch):
    with _session() as session:
        account = _seed_account(session)
        cleaned_hashes: list[str] = []

        def record_cleanup(_session_ciphertext, authorization_hash, _credentials):
            cleaned_hashes.append(authorization_hash)
            return SimpleNamespace(ok=True, detail="cleaned", failure_type="")

        monkeypatch.setattr(
            account_security_service.gateway,
            "list_authorizations",
            lambda *_args, **_kwargs: [
                _remote_authorization("primary", is_current=True, device_model="平台主控", platform="Linux", app_name="TG运营平台"),
                _remote_authorization("external-hash", device_model="Unknown", platform="Unknown", app_name="Legacy Client"),
            ],
        )
        monkeypatch.setattr(account_security_service.gateway, "cleanup_authorization", record_cleanup)
        batch = create_account_security_batch(
            session,
            1,
            AccountSecurityBatchCreate(account_ids=[account.id], action_types=["cleanup_devices"], confirm_text="确认"),
            "tester",
        )

        assert drain_account_security_batches(lambda: Session(session.bind), limit=10) == 1
        refreshed = account_security_batch_detail(session, 1, batch.id)

        assert cleaned_hashes == []
        assert refreshed.status == "manual_required"
        assert refreshed.items[0].status == "manual_required"
        assert refreshed.items[0].cleanup_status == "manual_required"
        assert refreshed.items[0].failure_type == "official_anchor_missing"


def test_device_cleanup_scan_failure_is_not_marked_success(monkeypatch):
    with _session() as session:
        account = _seed_account(session)

        def fail_authorization_scan(*_args, **_kwargs):
            raise RuntimeError("scan failed")

        monkeypatch.setattr(account_security_service.gateway, "list_authorizations", fail_authorization_scan)
        batch = create_account_security_batch(
            session,
            1,
            AccountSecurityBatchCreate(account_ids=[account.id], action_types=["cleanup_devices"], confirm_text="确认"),
            "tester",
        )

        assert drain_account_security_batches(lambda: Session(session.bind), limit=10) == 1
        refreshed = account_security_batch_detail(session, 1, batch.id)
        item = refreshed.items[0]

        assert refreshed.status == "failed"
        assert item.cleanup_status == "failed"
        assert "设备扫描失败" in item.failure_detail


def test_managed_two_fa_save_and_rotate_store_encrypted_password(monkeypatch):
    with _session() as session:
        account = _seed_account(session)
        saved = save_managed_two_fa_password(
            session,
            1,
            account.id,
            ManagedTwoFaRequest(password="old-password", reason="首次托管"),
            "tester",
        )
        calls: list[dict[str, str | None]] = []

        def set_two_fa(_session_ciphertext, password, **_kwargs):
            calls.append({"password": password, "current_password": _kwargs.get("current_password")})
            return SimpleNamespace(ok=True, status="enabled", detail="", failure_type="")

        monkeypatch.setattr(account_security_service.gateway, "set_two_fa_password", set_two_fa)
        rotated = rotate_managed_two_fa_password(
            session,
            1,
            account.id,
            ManagedTwoFaRequest(password="new-password", reason="轮换"),
            "tester",
        )
        snapshot = session.scalar(select(TgAccountSecuritySnapshot).where(TgAccountSecuritySnapshot.account_id == account.id))

        assert saved.two_fa_status == "enabled"
        assert rotated.two_fa_status == "enabled"
        assert calls == [{"password": "new-password", "current_password": "old-password"}]
        assert snapshot.two_fa_password_ciphertext
        assert snapshot.two_fa_password_ciphertext != "new-password"


def test_modal_confirmation_text_starts_batch_without_legacy_phrase():
    with _session() as session:
        account = _seed_account(session)
        payload = AccountSecurityBatchCreate(
            account_ids=[account.id],
            action_types=["update_profile"],
            confirm_text="确认",
            profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
            reason="测试弹窗确认",
        )

        batch = create_account_security_batch(session, 1, payload, "tester")

        assert batch.status == "running"
        assert batch.confirm_text == "确认"


def test_confirmed_profile_batch_uses_preview_overrides_without_regenerating(monkeypatch):
    with _session() as session:
        account = _seed_account(session)

        def fail_if_regenerated(*args, **kwargs):
            raise AssertionError("confirmed batch should reuse preview overrides")

        monkeypatch.setattr(account_security_service, "_generate_profiles", fail_if_regenerated)
        payload = AccountSecurityBatchCreate(
            account_ids=[account.id],
            action_types=["update_profile", "update_username"],
            confirm_text="确认",
            profile_strategy=ProfileGenerationStrategy(generation_mode="ai_random"),
            preview_overrides=[
                AccountSecurityProfileOverride(
                    account_id=account.id,
                    generated_display_name="锅巴洋芋",
                    generated_first_name="锅巴洋芋",
                    generated_bio="爱美食，尤其喜欢路边摊",
                    username_candidates=["guoba_yangyu"],
                )
            ],
            reason="测试复用预览创建批次",
        )

        batch = create_account_security_batch(session, 1, payload, "tester")

        assert batch.status == "running"
        assert batch.items[0].generated_display_name == "锅巴洋芋"
        assert batch.items[0].username_candidates == ["guoba_yangyu"]


def test_ai_profile_parser_drops_english_last_name_from_generated_nickname():
    raw = json.dumps(
        {
            "items": [
                {
                    "display_name": "锅巴洋芋",
                    "first_name": "锅巴洋芋",
                    "last_name": "Luis",
                    "bio": "看到有意思的会回两句",
                    "username_candidates": ["guoba_yangyu"],
                }
            ]
        },
        ensure_ascii=False,
    )

    items = account_security_service._parse_ai_profile_items(raw, 1, ProfileGenerationStrategy())

    assert items[0]["last_name"] == ""


def test_manual_required_or_missing_session_accounts_are_auto_skipped():
    with _session() as session:
        active = _seed_account(session)
        offline = TgAccount(
            id=12,
            tenant_id=1,
            display_name="等待验证码账号",
            phone_masked="139****0000",
            developer_app_id=1,
            developer_app_version=1,
            status=AccountStatus.WAITING_CODE.value,
            session_ciphertext="",
            health_score=20,
        )
        session.add(offline)
        session.commit()

        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[active.id, offline.id],
                action_types=["update_profile"],
                profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
            ),
        )

        offline_preview = next(item for item in preview.items if item.account_id == offline.id)
        assert preview.summary["executable"] == 1
        assert preview.summary["skipped"] == 1
        assert preview.summary["manual_required"] == 0
        assert offline_preview.precheck_status == "skipped"
        assert "账号未在线或缺少可用 session" in offline_preview.blockers

        batch = create_account_security_batch(
            session,
            1,
            AccountSecurityBatchCreate(
                account_ids=[active.id, offline.id],
                action_types=["update_profile"],
                confirm_text="确认",
                profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
                reason="测试自动跳过离线账号",
            ),
            "tester",
        )
        skipped_item = next(item for item in batch.items if item.account_id == offline.id)
        assert skipped_item.status == "skipped"
        assert skipped_item.precheck_status == "skipped"
        assert "账号未在线或缺少可用 session" in skipped_item.skipped_reason
        assert batch.skipped_count == 1


def test_waiting_account_security_item_is_retried_when_due(monkeypatch):
    with _session() as session:
        account = _seed_account(session)
        calls = {"count": 0}

        def cleanup_once_then_succeed(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return SimpleNamespace(ok=False, detail="FRESH_RESET_AUTHORISATION_FORBIDDEN 24 SESSION", failure_type="等待限制")
            return SimpleNamespace(ok=True, detail="cleaned", failure_type="")

        monkeypatch.setattr(account_security_service.gateway, "list_authorizations", lambda *_args, **_kwargs: _remote_cleanup_authorizations())
        monkeypatch.setattr(account_security_service.gateway, "cleanup_authorization", cleanup_once_then_succeed)
        payload = AccountSecurityBatchCreate(
            account_ids=[account.id],
            action_types=["cleanup_devices"],
            confirm_text="确认加固",
            reason="测试等待重试",
        )
        batch = create_account_security_batch(session, 1, payload, "tester")

        assert drain_account_security_batches(lambda: Session(session.bind), limit=10) == 1
        item = session.scalar(select(TgAccountSecurityBatchItem).where(TgAccountSecurityBatchItem.batch_id == batch.id))
        assert item.status == "waiting"
        item.next_retry_at = _now() - timedelta(seconds=1)
        session.commit()

        assert drain_account_security_batches(lambda: Session(session.bind), limit=10) == 1
        refreshed = account_security_batch_detail(session, 1, batch.id)
        assert refreshed.status == "succeeded"
        assert refreshed.items[0].cleanup_status == "succeeded"
        assert calls["count"] == 2


def test_username_taken_creates_partial_success_without_rolling_back_profile():
    with _session() as session:
        account = _seed_account(session)
        payload = AccountSecurityBatchCreate(
            account_ids=[account.id],
            action_types=["update_profile", "update_username"],
            confirm_text="确认加固",
            profile_strategy=ProfileGenerationStrategy(generation_mode="template", username_prefix_hint="taken"),
            reason="测试用户名失败",
        )
        batch = create_account_security_batch(session, 1, payload, "tester")

        drain_account_security_batches(lambda: Session(session.bind), limit=10)
        item = session.scalar(select(TgAccountSecurityBatchItem).where(TgAccountSecurityBatchItem.batch_id == batch.id))
        updated = session.get(TgAccount, account.id)

        assert item.status == "partial_success"
        assert item.profile_status == "succeeded"
        assert item.username_status == "failed"
        assert updated.display_name == "旧账号"
        assert updated.tg_first_name == item.generated_first_name


def test_preview_overrides_are_persisted_and_existing_profile_is_not_overwritten():
    with _session() as session:
        account = _seed_account(session)
        account.username = "existing_user"
        account.tg_first_name = "已有名"
        existing_avatar, _existing_path = save_avatar_bytes(tenant_id=account.tenant_id, account_id=account.id, content_type="image/png", data=b"existing")
        new_avatar, _new_path = save_avatar_bytes(tenant_id=account.tenant_id, account_id=account.id, content_type="image/png", data=b"new")
        account.avatar_object_key = existing_avatar
        session.commit()

        payload = AccountSecurityBatchCreate(
            account_ids=[account.id],
            action_types=["update_profile", "update_username", "update_avatar"],
            confirm_text="确认加固",
            profile_strategy=ProfileGenerationStrategy(generation_mode="template", overwrite_existing=False),
            avatar_strategy=AvatarStrategy(mode="sequential", avatar_sources=[f"avatar:{new_avatar}"]),
            preview_overrides=[
                AccountSecurityProfileOverride(
                    account_id=account.id,
                    generated_display_name="手工昵称",
                    generated_first_name="手工名",
                    username_candidates=["manual_user_001"],
                    avatar_source=f"avatar:{new_avatar}",
                )
            ],
            reason="测试预览编辑",
        )
        batch = create_account_security_batch(session, 1, payload, "tester")

        drain_account_security_batches(lambda: Session(session.bind), limit=10)
        item = session.scalar(select(TgAccountSecurityBatchItem).where(TgAccountSecurityBatchItem.batch_id == batch.id))
        updated = session.get(TgAccount, account.id)

        assert item.generated_display_name == "手工昵称"
        assert item.generated_first_name == "手工名"
        assert item.username_status == "skipped"
        assert item.avatar_status == "skipped"
        assert updated.username == "existing_user"
        assert updated.avatar_object_key == existing_avatar
        assert updated.tg_first_name == "已有名"


def test_profile_init_replaces_system_generated_display_name_with_generated_chinese_name():
    with _session() as session:
        account = _seed_account(session)
        account.display_name = "导入0519-0000-001"
        session.commit()
        payload = AccountSecurityBatchCreate(
            account_ids=[account.id],
            action_types=["update_profile"],
            confirm_text="确认加固",
            profile_strategy=ProfileGenerationStrategy(generation_mode="template"),
            reason="测试资料初始化名称",
        )
        batch = create_account_security_batch(session, 1, payload, "tester")

        drain_account_security_batches(lambda: Session(session.bind), limit=10)
        item = session.scalar(select(TgAccountSecurityBatchItem).where(TgAccountSecurityBatchItem.batch_id == batch.id))
        updated = session.get(TgAccount, account.id)

        assert updated.display_name == item.generated_display_name
        assert updated.display_name != "导入0519-0000-001"


def test_profile_init_replaces_remote_tg_name_when_platform_name_is_placeholder(monkeypatch):
    with _session() as session:
        account = _seed_account(session)
        account.display_name = "新托管账号"
        account.tg_first_name = "旧TG名"
        account.tg_last_name = "旧TG姓"
        session.commit()
        calls: list[dict[str, str]] = []

        def capture_update_profile(*args, **kwargs):
            calls.append(
                {
                    "first_name": kwargs["first_name"],
                    "last_name": kwargs["last_name"],
                    "bio": kwargs["bio"],
                }
            )
            return SimpleNamespace(ok=True, detail="profile updated", failure_type="")

        monkeypatch.setattr(account_security_service.gateway, "update_profile", capture_update_profile)
        payload = AccountSecurityBatchCreate(
            account_ids=[account.id],
            action_types=["update_profile"],
            confirm_text="确认加固",
            profile_strategy=ProfileGenerationStrategy(generation_mode="template", overwrite_existing=False),
            preview_overrides=[
                AccountSecurityProfileOverride(
                    account_id=account.id,
                    generated_display_name="锅巴洋芋",
                    generated_first_name="锅巴洋芋",
                    generated_last_name="",
                    generated_bio="看到有意思的会回两句",
                )
            ],
            reason="测试资料初始化同时更新TG名称",
        )
        batch = create_account_security_batch(session, 1, payload, "tester")

        drain_account_security_batches(lambda: Session(session.bind), limit=10)
        item = session.scalar(select(TgAccountSecurityBatchItem).where(TgAccountSecurityBatchItem.batch_id == batch.id))
        updated = session.get(TgAccount, account.id)

        assert item.profile_status == "succeeded"
        assert updated.display_name == "锅巴洋芋"
        assert updated.tg_first_name == "锅巴洋芋"
        assert updated.tg_last_name == ""
        assert calls == [{"first_name": "锅巴洋芋", "last_name": "", "bio": "看到有意思的会回两句"}]


def test_create_account_generates_import_time_phone_tail_sequence_name():
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            TelegramDeveloperApp(
                id=1,
                app_name="测试开发者应用",
                api_id=12345,
                api_hash_ciphertext=encrypt_secret("hash"),
                health_status="健康",
            )
        )
        session.commit()

        first = create_account(session, TgAccountCreate(tenant_id=1, display_name="新托管账号", phone_number="+8613800011234"), "tester")
        second = create_account(session, TgAccountCreate(tenant_id=1, display_name="", phone_number="+8613800015678"), "tester")

        assert first.display_name.endswith("-1234-001")
        assert second.display_name.endswith("-5678-002")
        assert first.display_name.startswith(f"导入{_now():%m%d}-")
