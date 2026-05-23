from __future__ import annotations

from datetime import timedelta
import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AiProvider, AccountStatus, Material, TelegramDeveloperApp, Tenant, TenantAiSetting, TgAccount, TgAccountSecurityBatchItem, TgAccountSecuritySnapshot
from app.schemas import TgAccountCreate
from app.schemas.account_security import AccountSecurityBatchCreate, AccountSecurityPrecheckRequest, AccountSecurityProfileOverride, AvatarStrategy, ProfileGenerationStrategy
from app.security import encrypt_secret, encrypt_session
from app.storage import save_avatar_bytes
import app.services.account_security.service as account_security_service
from app.services._common import _now
from app.services.account_security import (
    account_security_batch_detail,
    create_account_security_batch,
    drain_account_security_batches,
    precheck_account_security_batch,
    refresh_account_security,
)
from app.services.accounts import create_account


def _session():
    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return Session(engine)


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


def test_refresh_account_security_records_trusted_session_and_external_device():
    with _session() as session:
        account = _seed_account(session)

        snapshot = refresh_account_security(session, 1, account.id, "tester")

        assert snapshot.trusted_session_status == "confirmed"
        assert snapshot.two_fa_status == "missing"
        assert snapshot.external_authorization_count == 1
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


def test_waiting_account_security_item_is_retried_when_due(monkeypatch):
    with _session() as session:
        account = _seed_account(session)
        calls = {"count": 0}

        def cleanup_once_then_succeed(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return SimpleNamespace(ok=False, detail="FRESH_RESET_AUTHORISATION_FORBIDDEN 24 SESSION", failure_type="等待限制")
            return SimpleNamespace(ok=True, detail="cleaned", failure_type="")

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
