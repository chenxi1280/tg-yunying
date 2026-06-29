from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountStatus, AiAccountVoiceProfile, TelegramDeveloperApp, Tenant, TgAccount, TgAccountSecurityBatch, TgAccountSecurityBatchItem, TgLoginFlow
from app.security import encrypt_secret, encrypt_session
from app.services import account_profile_auto_init
from app.services import accounts as accounts_service

pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_tenant_and_app(session: Session) -> None:
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


def _seed_login_account(session: Session, *, display_name: str = "John Smith", first_name: str = "John") -> TgAccount:
    _seed_tenant_and_app(session)
    account = TgAccount(
        id=11,
        tenant_id=1,
        display_name=display_name,
        tg_first_name=first_name,
        tg_last_name="Smith" if first_name == "John" else "",
        phone_masked="138****0000",
        developer_app_id=1,
        status=AccountStatus.WAITING_CODE.value,
        health_score=80,
    )
    session.add(account)
    session.add(TgLoginFlow(tenant_id=1, account_id=11, method="code", status=AccountStatus.WAITING_CODE.value))
    session.commit()
    return account


def _stub_successful_login(monkeypatch) -> None:
    monkeypatch.setattr(accounts_service, "credentials_for_account", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(
        accounts_service.gateway,
        "finish_login",
        lambda *_args, **_kwargs: (AccountStatus.ACTIVE.value, "raw-session"),
    )
    monkeypatch.setattr(accounts_service, "run_account_sync_now", lambda *_args, **_kwargs: [])


def test_verify_login_queues_chinese_profile_initialization_for_english_account(monkeypatch):
    with _session() as session:
        account = _seed_login_account(session)
        _stub_successful_login(monkeypatch)

        accounts_service.verify_login(session, account.id, "12345", None, actor="tester")

        batch = session.scalar(select(TgAccountSecurityBatch))
        item = session.scalar(select(TgAccountSecurityBatchItem))
        assert batch is not None
        assert item is not None
        assert batch.status == "running"
        assert batch.confirmed_by == "tester"
        assert batch.reason == "登录成功后自动初始化账号中文资料和头像"
        assert batch.overwrite_existing_profile is True
        assert batch.action_types == '["update_profile", "update_username", "update_avatar"]'
        assert '"generation_mode": "local_random"' in batch.profile_strategy
        assert '"mode":"material_random"' in batch.avatar_strategy
        assert item.account_id == account.id
        assert item.status == "pending"
        assert item.generated_display_name
        assert not any("A" <= char <= "z" for char in item.generated_display_name)


def test_verify_login_initializes_missing_ai_voice_profile(monkeypatch):
    with _session() as session:
        account = _seed_login_account(session)
        _stub_successful_login(monkeypatch)

        def fake_ensure(_session, tenant_id: int, account_ids: list[int]):
            assert tenant_id == 1
            session.add(
                AiAccountVoiceProfile(
                    tenant_id=tenant_id,
                    account_id=account_ids[0],
                    short_prompt_summary="青年短句，先观望再追问，偶尔说我看看",
                    status="active",
                    quality_status="active",
                )
            )
            return 1

        monkeypatch.setattr(account_profile_auto_init, "_ensure_voice_profiles", fake_ensure)

        accounts_service.verify_login(session, account.id, "12345", None, actor="tester")

        voice_profile = session.scalar(select(AiAccountVoiceProfile))
        assert voice_profile is not None
        assert voice_profile.account_id == account.id
        assert voice_profile.short_prompt_summary == "青年短句，先观望再追问，偶尔说我看看"


def test_qr_login_queues_profile_initialization_after_success(monkeypatch):
    with _session() as session:
        account = _seed_login_account(session)
        session.query(TgLoginFlow).delete()
        session.add(TgLoginFlow(tenant_id=1, account_id=account.id, method="qr", status=AccountStatus.WAITING_QR.value))
        session.commit()
        _stub_successful_login(monkeypatch)

        accounts_service.check_qr_login(session, account.id, actor="tester")

        batch_count = session.scalar(select(func.count(TgAccountSecurityBatch.id)))
        assert batch_count == 1


def test_login_does_not_queue_profile_initialization_when_profile_is_ready(monkeypatch):
    with _session() as session:
        account = _seed_login_account(session, display_name="锅巴洋芋", first_name="锅巴洋芋")
        account.username = "guoba_yangyu"
        account.avatar_object_key = "avatars/1/11/current.jpg"
        session.commit()
        _stub_successful_login(monkeypatch)

        accounts_service.verify_login(session, account.id, "12345", None, actor="tester")

        batch = session.scalar(select(TgAccountSecurityBatch))
        assert batch is None


def test_profile_reconcile_script_applies_when_only_voice_profiles_are_missing():
    module = _load_profile_reconcile_script()

    before = {
        "not_ready_count": 0,
        "not_ready_account_ids": [],
        "missing_voice_profile_count": 2,
        "missing_voice_profile_account_ids": [11, 12],
    }

    assert module._should_apply_reconcile(before)
    assert module._reconcile_account_ids(before) == [11, 12]


def test_profile_reconcile_script_fails_when_voice_profiles_remain_missing():
    module = _load_profile_reconcile_script()

    before = {"missing_voice_profile_count": 2}
    after = {"missing_voice_profile_count": 2}

    with pytest.raises(RuntimeError, match="did not complete"):
        module._assert_reconcile_effective(before, after, True)

    module._assert_reconcile_effective(before, {"missing_voice_profile_count": 0}, True)
    module._assert_reconcile_effective(before, after, False)


def test_profile_reconcile_script_commits_voice_profiles_per_batch(monkeypatch):
    module = _load_profile_reconcile_script()
    committed_batches = []

    class FakeSession:
        batch = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def commit(self):
            committed_batches.append(list(self.batch or []))

    def fake_session_factory():
        return FakeSession()

    def fake_generate_voice_profiles_with_ai(session, *, tenant_id):
        return object()

    def fake_ensure(session, *, tenant_id, account_ids, generator):
        if account_ids == [13, 14]:
            raise RuntimeError("AI provider HTTP 429: quota exhausted")
        session.batch = list(account_ids)
        return len(account_ids)

    monkeypatch.setattr(module, "VOICE_PROFILE_COMMIT_CHUNK_SIZE", 2)
    monkeypatch.setattr(module, "SessionLocal", fake_session_factory)
    monkeypatch.setattr(module, "generate_voice_profiles_with_ai", fake_generate_voice_profiles_with_ai)
    monkeypatch.setattr(module, "ensure_voice_profiles_for_accounts", fake_ensure)

    result = module._reconcile_voice_profiles([11, 12, 13, 14])

    assert result["created"] == 2
    assert result["completed_account_ids"] == [11, 12]
    assert result["failed_batch_account_ids"] == [13, 14]
    assert result["error"]["message"] == "AI provider HTTP 429: quota exhausted"
    assert committed_batches == [[11, 12]]


def _load_profile_reconcile_script():
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    script_path = Path(__file__).resolve().parents[2] / ".github/scripts/account_profile_initialization_reconcile.py"
    spec = spec_from_file_location("account_profile_initialization_reconcile", script_path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
