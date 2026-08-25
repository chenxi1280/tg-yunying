from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.authorization_dr import online_abc_runner as runner
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.online_abc_chunk import (
    MAX_CHUNK_ACCOUNTS,
    require_chunk_size,
)

pytestmark = pytest.mark.no_postgres


def test_runner_pauses_after_exactly_thirty_accounts(monkeypatch) -> None:
    state = {"status": "running", "next_account": 0}

    monkeypatch.setattr(runner, "_batch", lambda *_args: SimpleNamespace())
    monkeypatch.setattr(runner, "_require_batch_contract", lambda *_args: None)
    monkeypatch.setattr(runner, "resume_online_abc_chunk", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(runner, "sync_online_abc_batch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "_run_current_item", lambda *_args, **_kwargs: None)

    def status(*_args) -> dict:
        return {"batch": {"status": state["status"]}}

    def start(*_args, **_kwargs) -> dict:
        state["next_account"] += 1
        return {"account_id": state["next_account"]}

    def pause(*_args, **_kwargs) -> bool:
        state["status"] = "stopped"
        return True

    monkeypatch.setattr(runner, "online_abc_runner_status", status)
    monkeypatch.setattr(runner, "start_next_online_abc_item", start)
    monkeypatch.setattr(runner, "pause_online_abc_chunk", pause)

    result = runner.run_online_abc_batch(
        object(),
        "batch-30",
        requested_by="operator",
        approved_by="approver",
        approval_ref="ABC-FULL-30",
        runtime_release_sha="a" * 40,
        max_accounts=MAX_CHUNK_ACCOUNTS,
        sleeper=lambda _seconds: None,
    )

    assert MAX_CHUNK_ACCOUNTS == 30
    assert result["batch"]["status"] == "stopped"
    assert result["chunk"] == {
        "max_accounts": 30,
        "processed_count": 30,
        "account_ids": list(range(1, 31)),
    }


def test_runner_rejects_more_than_thirty_accounts() -> None:
    with pytest.raises(AuthorizationDrError) as exc_info:
        require_chunk_size(31)

    assert exc_info.value.code == "online_abc_chunk_size_invalid"
