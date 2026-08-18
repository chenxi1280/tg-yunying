from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPool,
    AccountStatus,
    Tenant,
    TgAccount,
    TgAccountSecurityBatch,
    TgAccountSecurityBatchItem,
)
from app.security import encrypt_session


pytestmark = pytest.mark.no_postgres

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_script():
    path = PROJECT_ROOT / ".github/scripts/account_profile_duplicate_reconcile.py"
    spec = spec_from_file_location("account_profile_duplicate_reconcile", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workflow_inputs(path: Path) -> dict:
    workflow = yaml.safe_load(path.read_text())
    return workflow["on"]["workflow_dispatch"]["inputs"]


def test_identity_operations_use_separate_bounded_workflow():
    deploy_inputs = _workflow_inputs(PROJECT_ROOT / ".github/workflows/deploy-production.yml")
    identity_inputs = _workflow_inputs(
        PROJECT_ROOT / ".github/workflows/production-account-profile-identity.yml"
    )

    assert len(deploy_inputs) <= 25
    assert "run_account_profile_dedupe" not in deploy_inputs
    assert "run_avatar_material_import" not in deploy_inputs
    assert set(identity_inputs) == {
        "operation",
        "mode",
        "seed",
        "login_batch_id",
        "expected_target_count",
        "style_group_ids",
        "deployed_sha",
        "expected_manifest_sha256",
        "approval_ref",
    }


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(AccountPool(id=1, tenant_id=1, name="普通账号池", pool_purpose="normal", is_default=True))
    session.commit()
    return session


def _account(account_id: int, name: str) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=1,
        pool_id=1,
        account_identity="normal",
        display_name=name,
        phone_masked=f"138****{account_id:04d}",
        status=AccountStatus.ACTIVE.value,
        session_ciphertext=encrypt_session(f"session-{account_id}"),
        profile_sync_status="已同步",
        avatar_object_key=f"avatars/1/{account_id}/current.jpg",
    )


def test_manifest_targets_only_duplicate_non_keepers_and_is_stable():
    script = _load_script()
    with _session() as session:
        session.add_all([_account(1, "海盐日记"), _account(2, "海盐日记"), _account(3, "唯一昵称")])
        session.commit()

        first = script.build_manifest(session, tenant_id=1, seed="fixed", deployed_sha="abc123")
        second = script.build_manifest(session, tenant_id=1, seed="fixed", deployed_sha="abc123")

    assert first == second
    assert first["duplicate_group_count"] == 1
    assert first["rename_target_count"] == 1
    assert first["keepers"] == [1]
    assert [target["account_id"] for target in first["targets"]] == [2]
    assert first["targets"][0]["new_display_name"] not in {"海盐日记", "唯一昵称"}
    assert script.manifest_sha256(first) == script.manifest_sha256(second)


def test_reconcile_summary_keeps_hash_and_counts_without_targets():
    script = _load_script()
    payload = {
        "mode": "preview",
        "manifest_sha256": "a" * 64,
        "manifest": {
            "active_operational_account_count": 892,
            "duplicate_group_count": 49,
            "duplicate_account_count": 533,
            "rename_target_count": 484,
            "targets": [{"account_id": account_id} for account_id in range(484)],
        },
        "batch_ids": [],
        "after_duplicate_group_count": 49,
        "after_rename_target_count": 484,
    }

    summary = script._reconcile_summary(payload)

    assert summary["manifest_sha256"] == "a" * 64
    assert summary["manifest_sha256_b64"] == script._sha256_b64("a" * 64)
    assert summary["rename_target_count"] == 484
    assert "manifest" not in summary


def test_readback_status_counts_exposes_worker_outcomes():
    script = _load_script()
    rows = [
        (
            SimpleNamespace(status="running"),
            SimpleNamespace(status="pending", profile_status="pending", failure_type=""),
            _account(1, "海盐日记"),
        ),
        (
            SimpleNamespace(status="partial_success"),
            SimpleNamespace(status="failed", profile_status="failed", failure_type="执行异常"),
            _account(2, "云边散步"),
        ),
    ]

    counts = script._readback_status_counts(rows)

    assert counts["item_status_counts"] == {"failed": 1, "pending": 1}
    assert counts["failure_type_counts"] == {"执行异常": 1}


def test_assert_unchanged_rejects_old_name_drift():
    script = _load_script()
    with _session() as session:
        session.add(_account(1, "已经变化"))
        session.commit()

        with pytest.raises(RuntimeError, match="target state drift"):
            script._assert_unchanged(
                session,
                1,
                [
                    {
                        "account_id": 1,
                        "old_display_name": "旧名字",
                        "old_profile_sync_status": "已同步",
                        "old_account_status": AccountStatus.ACTIVE.value,
                        "old_account_identity": "normal",
                    }
                ],
            )


def test_existing_manifest_batch_is_idempotently_reused():
    script = _load_script()
    target = {"account_id": 2, "new_display_name": "云边散步"}
    with _session() as session:
        batch = TgAccountSecurityBatch(
            tenant_id=1,
            reason=script._batch_reason("a" * 64, 1) + "approval-ref",
        )
        session.add(batch)
        session.flush()
        session.add(
            TgAccountSecurityBatchItem(
                batch_id=batch.id,
                tenant_id=1,
                account_id=2,
                generated_display_name="云边散步",
            )
        )
        session.commit()
        batch_id = int(batch.id)

        batch_ids, account_ids = script._existing_batch_state(session, 1, "a" * 64, [target])

    assert batch_ids == [batch_id]
    assert account_ids == {2}


def test_remote_readback_requires_exact_first_name_and_empty_last_name(monkeypatch):
    script = _load_script()
    item = TgAccountSecurityBatchItem(
        account_id=1,
        status="succeeded",
        profile_status="succeeded",
        generated_display_name="海边走走",
    )
    account = _account(1, "海边走走")
    monkeypatch.setattr(script, "credentials_for_account", lambda _session, _account: object())
    monkeypatch.setattr(
        script.gateway,
        "pull_profile",
        lambda *_args, **_kwargs: SimpleNamespace(first_name="海边", last_name="走走"),
    )

    with _session() as session:
        result = script._read_remote_profile(session, item, account)

    assert result["status"] == "mismatched"
    assert result["actual_display_name"] == "海边 走走"


def test_remote_readback_recounts_duplicates_without_regenerating_names(monkeypatch):
    script = _load_script()
    batch = SimpleNamespace(id=541, status="succeeded")
    item = SimpleNamespace(status="succeeded", profile_status="succeeded", failure_type="")
    rows = [(batch, item, _account(1, "海边走走"))]

    monkeypatch.setattr(script, "SessionLocal", _session)
    monkeypatch.setattr(script, "build_manifest", lambda *_args, **_kwargs: pytest.fail("readback must not generate names"))
    monkeypatch.setattr(script, "_current_duplicate_counts", lambda *_args: {"duplicate_group_count": 0, "rename_target_count": 0})
    monkeypatch.setattr(script, "_readback_rows", lambda *_args: rows)
    monkeypatch.setattr(script, "_readback_target_count", lambda _rows: 1)
    monkeypatch.setattr(script, "_read_remote_profile", lambda *_args: {"status": "matched"})

    result = script.remote_readback()

    assert result["complete"] is True
    assert result["remote_matched_count"] == 1
