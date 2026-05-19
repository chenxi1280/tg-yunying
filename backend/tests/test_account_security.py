from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import AccountStatus, TelegramDeveloperApp, Tenant, TgAccount, TgAccountSecurityBatchItem, TgAccountSecuritySnapshot
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


def _session():
    engine = create_engine("sqlite:///:memory:", future=True, connect_args={"check_same_thread": False}, poolclass=StaticPool)
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


def test_precheck_uses_ai_random_profile_preview_and_flags_offline_account():
    with _session() as session:
        account = _seed_account(session, status=AccountStatus.NEED_RELOGIN.value, session_value="")

        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[account.id],
                action_types=["update_profile", "update_username"],
                profile_strategy=ProfileGenerationStrategy(generation_mode="ai_random", forbidden_words=["违规"]),
            ),
        )

        item = preview.items[0]
        assert preview.summary["manual_required"] == 1
        assert item.precheck_status == "manual_required"
        assert item.generated_display_name
        assert item.username_candidates
        assert "账号未在线或缺少可用 session" in item.blockers
        assert any("AI 随机命名重试后仍不可用" in blocker for blocker in item.blockers)


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
