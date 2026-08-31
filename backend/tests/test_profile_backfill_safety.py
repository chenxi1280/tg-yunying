from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from app.models import TgAccount
from scripts import backfill_account_natural_profiles as backfill
from scripts import test_copy_group_profiles as profile_copy


pytestmark = pytest.mark.no_postgres


def _manifest_item(account_id: int) -> dict[str, object]:
    return {
        "account_id": account_id,
        "tenant_id": 1,
        "proposed_display_name": f"用户{account_id}",
        "proposed_first_name": f"用户{account_id}",
        "proposed_last_name": "",
        "proposed_bio": "",
        "proposed_avatar_source": f"material:{account_id}",
    }


def test_apply_rejects_tampered_manifest_before_database_access(tmp_path, monkeypatch):
    manifest = backfill.build_manifest([_manifest_item(1)], tenant_id=1)
    manifest["items"][0]["proposed_display_name"] = "被篡改"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        backfill,
        "SessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be opened")),
    )

    with pytest.raises(ValueError, match="manifest_hash_mismatch"):
        backfill.run_apply(str(manifest_path))


def test_apply_cli_does_not_truncate_manifest(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run_apply(*, manifest_path: str) -> None:
        captured["manifest_path"] = manifest_path

    monkeypatch.setattr(backfill, "run_apply", fake_run_apply)
    monkeypatch.setattr(
        sys,
        "argv",
        ["backfill", "--mode", "apply", "--manifest-file", "approved.json"],
    )

    backfill.main()

    assert captured == {"manifest_path": "approved.json"}


def test_preview_requires_explicit_tenant_before_database_access(monkeypatch):
    monkeypatch.setattr(
        backfill,
        "SessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be opened")),
    )

    with pytest.raises(ValueError, match="tenant_id_required"):
        backfill.run_preview()


def test_manifest_rejects_cross_tenant_item():
    manifest = backfill.build_manifest([_manifest_item(1)], tenant_id=1)
    manifest["items"][0]["tenant_id"] = 2

    with pytest.raises(ValueError, match="manifest_tenant_mismatch"):
        backfill._validated_manifest_items(manifest)


def test_apply_chunks_tenant_items_at_fifty(tmp_path, monkeypatch):
    manifest = backfill.build_manifest([_manifest_item(i) for i in range(1, 52)], tenant_id=1)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    chunk_sizes = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(backfill, "SessionLocal", FakeSession)
    monkeypatch.setattr(
        backfill,
        "_execute_tenant_batch",
        lambda _session, _tenant_id, items: chunk_sizes.append(len(items)),
    )

    backfill.run_apply(str(manifest_path))

    assert chunk_sizes == [50, 1]


def test_profile_plan_requires_authentic_candidates():
    account = TgAccount(id=1, tenant_id=1, phone_masked="138****0001")

    with pytest.raises(ValueError, match="profile_candidate_source_empty"):
        backfill.generate_plan([account], [], avatar_sources=["material:1"])


def test_profile_copy_defaults_to_database_mode(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_run_pipeline(mode: str, sample_limit: int, target_accounts: int):
        captured.update(
            mode=mode,
            sample_limit=sample_limit,
            target_accounts=target_accounts,
        )
        return {"status": "success", "counts": {}, "allocations": []}

    monkeypatch.setattr(profile_copy, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(profile_copy, "print_report", lambda _results: None)
    monkeypatch.setattr(sys, "argv", ["profile-copy"])

    profile_copy.main()

    assert captured["mode"] == "database"


def test_profile_copy_live_mode_requires_exact_account(monkeypatch):
    called = False

    async def fake_run_pipeline(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(profile_copy, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(sys, "argv", ["profile-copy", "--mode", "telethon"])

    with pytest.raises(SystemExit):
        profile_copy.main()

    assert called is False


def test_generate_plan_preserves_bio_and_freezes_avatar_source():
    accounts = [
        TgAccount(id=i, tenant_id=1, phone_masked=f"138****{i:04d}", tg_bio=f"原简介{i}")
        for i in range(2)
    ]
    candidates = [
        backfill.NaturalCandidateProfile(
            group_title="Active Group",
            user_id=f"100{i}",
            username=f"user_{i}",
            display_name=f"老哥{i}",
            first_name=f"老哥{i}",
            last_name="",
            bio="",
        )
        for i in range(2)
    ]
    plan = backfill.generate_plan(
        accounts,
        candidates,
        avatar_sources=["material:7", "material:8"],
    )

    assert [item["proposed_bio"] for item in plan] == ["原简介0", "原简介1"]
    assert [item["proposed_avatar_source"] for item in plan] == ["material:7", "material:8"]
    assert all("proposed_username_candidates" not in item for item in plan)
    assert all("copied_from_user" not in item for item in plan)


def test_execute_tenant_batch_uses_exact_top_level_overrides(monkeypatch):
    captured = {}

    def fake_create(_session, tenant_id, payload, actor):
        captured.update(tenant_id=tenant_id, payload=payload, actor=actor)
        return SimpleNamespace(id=77, status="running")

    monkeypatch.setattr(backfill, "create_account_security_batch", fake_create)
    backfill._execute_tenant_batch(object(), 1, [_manifest_item(11)])

    payload = captured["payload"]
    assert payload.action_types == ["update_profile", "update_avatar"]
    assert payload.profile_strategy.username_enabled is False
    assert payload.avatar_strategy.mode == "none"
    assert payload.preview_overrides[0].generated_display_name == "用户11"
    assert payload.preview_overrides[0].avatar_source == "material:11"
