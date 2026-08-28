from __future__ import annotations

import json
import sys

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
        "proposed_username_candidates": [f"natural_user_{account_id}"],
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


def test_profile_plan_requires_authentic_candidates():
    account = TgAccount(id=1, tenant_id=1, phone_masked="138****0001")

    with pytest.raises(ValueError, match="profile_candidate_source_empty"):
        backfill.generate_plan([account], [])


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
