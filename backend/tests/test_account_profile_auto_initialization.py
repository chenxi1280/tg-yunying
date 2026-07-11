from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountPool, AccountStatus, AiAccountVoiceProfile, TelegramDeveloperApp, Tenant, TgAccount, TgAccountSecurityBatch, TgAccountSecurityBatchItem, TgLoginFlow
from app.schemas.account_security import AccountSecurityPrecheckRequest, ProfileGenerationStrategy
from app.security import encrypt_secret, encrypt_session
from app.services import account_profile_auto_init
from app.services import accounts as accounts_service
import app.services.account_security.service as account_security_service
from app.services.account_security import precheck_account_security_batch

pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_tenant_and_app(session: Session) -> None:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(AccountPool(id=1, tenant_id=1, name="普通账号池", pool_purpose="normal", is_default=True))
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
        pool_id=1,
        account_identity="normal",
        status=AccountStatus.WAITING_CODE.value,
        health_score=80,
    )
    session.add(account)
    session.add(TgLoginFlow(tenant_id=1, account_id=11, method="code", status=AccountStatus.WAITING_CODE.value))
    session.commit()
    return account


def _assign_pool(session: Session, account: TgAccount, *, pool_purpose: str, account_identity: str) -> None:
    pool_id = {"normal": 1, "code_receiver": 2, "rank_deboost": 3}[pool_purpose]
    system_key = pool_purpose if pool_purpose in {"code_receiver", "rank_deboost"} else ""
    if session.get(AccountPool, pool_id) is None:
        session.add(AccountPool(id=pool_id, tenant_id=1, name=f"{pool_purpose}账号池", pool_purpose=pool_purpose, system_key=system_key))
    account.pool_id = pool_id
    account.account_identity = account_identity
    session.commit()


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


def test_login_skips_profile_and_voice_initialization_for_code_receiver(monkeypatch):
    with _session() as session:
        account = _seed_login_account(session)
        _assign_pool(session, account, pool_purpose="code_receiver", account_identity="code_receiver")
        _stub_successful_login(monkeypatch)

        def fail_voice_profile_init(*_args, **_kwargs):
            raise AssertionError("code receiver must not create voice profile")

        monkeypatch.setattr(account_profile_auto_init, "_ensure_voice_profiles", fail_voice_profile_init)

        accounts_service.verify_login(session, account.id, "12345", None, actor="tester")

        assert session.scalar(select(TgAccountSecurityBatch)) is None
        assert session.scalar(select(AiAccountVoiceProfile)) is None


@pytest.mark.parametrize(
    ("pool_purpose", "account_identity"),
    [("rank_deboost", "rank_deboost"), ("rank_deboost", "normal")],
)
def test_login_skips_profile_and_voice_initialization_for_non_normal_usage(monkeypatch, pool_purpose, account_identity):
    with _session() as session:
        account = _seed_login_account(session)
        _assign_pool(session, account, pool_purpose=pool_purpose, account_identity=account_identity)
        _stub_successful_login(monkeypatch)

        def fail_voice_profile_init(*_args, **_kwargs):
            raise AssertionError("non-normal account must not create voice profile")

        monkeypatch.setattr(account_profile_auto_init, "_ensure_voice_profiles", fail_voice_profile_init)

        accounts_service.verify_login(session, account.id, "12345", None, actor="tester")

        assert session.scalar(select(TgAccountSecurityBatch)) is None
        assert session.scalar(select(AiAccountVoiceProfile)) is None


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


def test_local_profile_batch_names_do_not_cluster_on_four_to_six_characters():
    with _session() as session:
        _seed_tenant_and_app(session)
        accounts = [
            TgAccount(
                    id=index,
                    tenant_id=1,
                    pool_id=1,
                    account_identity="normal",
                    display_name=f"导入0524-8740-{index:03d}",
                phone_masked=f"138****{index:04d}",
                developer_app_id=1,
                status=AccountStatus.ACTIVE.value,
                session_ciphertext=encrypt_session("session"),
                health_score=90,
            )
            for index in range(1, 61)
        ]
        session.add_all(accounts)
        session.commit()

        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[account.id for account in accounts],
                action_types=["update_profile", "update_username"],
                profile_strategy=ProfileGenerationStrategy(generation_mode="local_random"),
            ),
        )

        names = [item.generated_display_name for item in preview.items]
        clustered_count = sum(1 for name in names if 4 <= len(name) <= 6)
        assert len(set(names)) == 60
        assert any(len(name) <= 3 for name in names)
        assert any(len(name) >= 7 for name in names)
        assert clustered_count <= 45


def test_account_security_precheck_skips_code_receiver_profile_and_2fa(monkeypatch):
    with _session() as session:
        _seed_tenant_and_app(session)
        account = TgAccount(
            id=11,
            tenant_id=1,
            display_name="John Smith",
            tg_first_name="John",
            phone_masked="138****0000",
            developer_app_id=1,
            pool_id=2,
            status=AccountStatus.ACTIVE.value,
            account_identity="code_receiver",
            session_ciphertext=encrypt_session("session"),
            health_score=90,
        )
        session.add(AccountPool(id=2, tenant_id=1, name="接码账号池", pool_purpose="code_receiver", system_key="code_receiver"))
        session.add(account)
        session.commit()

        def fail_security_refresh(*_args, **_kwargs):
            raise AssertionError("code receiver maintenance precheck should not refresh security")

        monkeypatch.setattr(account_security_service, "refresh_account_security", fail_security_refresh)

        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[account.id],
                action_types=["update_profile", "update_username", "update_avatar", "set_two_fa"],
                profile_strategy=ProfileGenerationStrategy(generation_mode="local_random"),
            ),
        )

        item = preview.items[0]
        assert preview.summary["executable"] == 0
        assert preview.summary["skipped"] == 1
        assert item.precheck_status == "skipped"
        assert "接码专用账号只允许接收验证码" in item.blockers


def test_account_security_precheck_skips_rank_deboost_and_mismatch_profile_mutations(monkeypatch):
    with _session() as session:
        normal = _seed_login_account(session)
        rank = TgAccount(
            id=12,
            tenant_id=1,
            display_name="降权观察号",
            phone_masked="138****0012",
            developer_app_id=1,
            pool_id=3,
            account_identity="rank_deboost",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext=encrypt_session("rank"),
            health_score=90,
        )
        mismatch = TgAccount(
            id=13,
            tenant_id=1,
            display_name="错配号",
            phone_masked="138****0013",
            developer_app_id=1,
            pool_id=3,
            account_identity="normal",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext=encrypt_session("mismatch"),
            health_score=90,
        )
        session.add(AccountPool(id=3, tenant_id=1, name="降权观察池", pool_purpose="rank_deboost", system_key="rank_deboost"))
        normal.status = AccountStatus.ACTIVE.value
        normal.session_ciphertext = encrypt_session("normal")
        session.add_all([rank, mismatch])
        session.commit()

        def fail_security_refresh(*_args, **_kwargs):
            raise AssertionError("profile-only precheck should not refresh security")

        monkeypatch.setattr(account_security_service, "refresh_account_security", fail_security_refresh)

        preview = precheck_account_security_batch(
            session,
            1,
            AccountSecurityPrecheckRequest(
                account_ids=[normal.id, rank.id, mismatch.id],
                action_types=["update_profile", "update_username"],
                profile_strategy=ProfileGenerationStrategy(generation_mode="local_random"),
            ),
        )

        statuses = {item.account_id: item for item in preview.items}
        assert preview.summary["executable"] == 1
        assert preview.summary["skipped"] == 2
        assert statuses[normal.id].precheck_status == "executable"
        assert statuses[rank.id].precheck_status == "skipped"
        assert statuses[mismatch.id].precheck_status == "skipped"
        assert "账号用途不允许执行账号资料/安全变更" in statuses[rank.id].blockers
        assert "账号用途不一致，已禁止执行账号资料/安全变更" in statuses[mismatch.id].blockers


def test_account_security_worker_blocks_code_receiver_profile_and_2fa(monkeypatch):
    with _session() as session:
        _seed_tenant_and_app(session)
        account = TgAccount(
            id=11,
            tenant_id=1,
            display_name="John Smith",
            phone_masked="138****0000",
            developer_app_id=1,
            pool_id=2,
            status=AccountStatus.ACTIVE.value,
            account_identity="code_receiver",
            session_ciphertext=encrypt_session("session"),
            health_score=90,
        )
        batch = TgAccountSecurityBatch(
            id=1,
            tenant_id=1,
            action_types='["update_profile", "set_two_fa"]',
            status="running",
            total_count=1,
        )
        item = TgAccountSecurityBatchItem(
            id=1,
            batch_id=1,
            tenant_id=1,
            account_id=11,
            status="pending",
            precheck_status="executable",
            profile_status="pending",
            two_fa_status="pending",
        )
        session.add(AccountPool(id=2, tenant_id=1, name="接码账号池", pool_purpose="code_receiver", system_key="code_receiver"))
        session.add_all([account, batch, item])
        session.commit()

        def fail_credentials(*_args, **_kwargs):
            raise AssertionError("code receiver worker should block before credentials lookup")

        monkeypatch.setattr(account_security_service, "credentials_for_account", fail_credentials)

        account_security_service._execute_batch_item(session, item.id)

        blocked = session.get(TgAccountSecurityBatchItem, item.id)
        assert blocked.status == "skipped"
        assert blocked.failure_type == "code_receiver_reserved"
        assert blocked.profile_status == "skipped"
        assert blocked.two_fa_status == "skipped"


def test_ai_profile_parser_rejects_english_mixed_display_fields():
    strategy = ProfileGenerationStrategy(generation_mode="ai_random")
    raw = json.dumps(
        {
            "items": [
                {
                    "display_name": "小满 SunshineDailyLong",
                    "first_name": "小满 SunshineDailyLong",
                    "last_name": "VeryLongEnglishName",
                    "bio": "hello there",
                    "username_candidates": ["xiaoman_daily"],
                },
                {
                    "display_name": "锅巴洋芋",
                    "first_name": "锅巴洋芋",
                    "last_name": "",
                    "bio": "看到有意思的会回两句",
                    "username_candidates": ["guoba_yangyu"],
                },
            ]
        },
        ensure_ascii=False,
    )

    with pytest.raises(RuntimeError, match="AI 生成资料不足"):
        account_security_service._parse_ai_profile_items(raw, 2, strategy)


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
