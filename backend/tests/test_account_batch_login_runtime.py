from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import _validate_account_batch_login_settings


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _runtime_settings(
    mode: str,
    *,
    host_concurrency: int = 1,
    developer_concurrency: int = 1,
    worker_concurrency: int = 4,
):
    return SimpleNamespace(
        account_batch_login_mode=mode,
        account_batch_login_max_lines=100,
        account_batch_login_item_deadline_seconds=300,
        account_batch_login_code_wait_seconds=120,
        account_batch_login_poll_interval_seconds=3,
        account_batch_login_credential_ttl_seconds=86400,
        account_batch_login_reconcile_seconds=86400,
        account_batch_login_worker_concurrency=worker_concurrency,
        account_post_login_init_mode="enabled",
        account_post_login_init_secret_ttl_seconds=900,
        account_post_login_init_worker_concurrency=2,
        account_batch_login_host_concurrency=host_concurrency,
        account_batch_login_host_min_interval_seconds=3,
        account_batch_login_developer_app_concurrency=developer_concurrency,
        account_batch_phone_fingerprint_version=1,
        account_batch_phone_fingerprint_versions="1",
    )


def test_batch_login_runtime_configuration_fails_closed() -> None:
    _validate_account_batch_login_settings(_runtime_settings("off", host_concurrency=0, developer_concurrency=0))
    with pytest.raises(ValueError, match="reconciliation"):
        _validate_account_batch_login_settings(_runtime_settings("reconcile_only", developer_concurrency=0))
    with pytest.raises(ValueError, match="HOST_CONCURRENCY"):
        _validate_account_batch_login_settings(_runtime_settings("enabled", host_concurrency=0))
    with pytest.raises(ValueError, match="WORKER_CONCURRENCY"):
        _validate_account_batch_login_settings(_runtime_settings("enabled", worker_concurrency=0))


def test_batch_login_runtime_allows_200_line_batches() -> None:
    settings = _runtime_settings("enabled")
    settings.account_batch_login_max_lines = 200
    _validate_account_batch_login_settings(settings)
    settings.account_batch_login_max_lines = 201
    with pytest.raises(ValueError, match="between 1 and 200"):
        _validate_account_batch_login_settings(settings)


@pytest.mark.parametrize(
    ("mode", "expected", "batch_calls"),
    [("off", 0, 0), ("reconcile_only", 5, 0), ("enabled", 12, 1)],
)
def test_account_login_worker_mode_controls_new_phases(monkeypatch, mode, expected, batch_calls) -> None:
    from app import worker

    calls = {"batch": 0}
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(account_batch_login_mode=mode))
    monkeypatch.setattr(worker, "drain_account_post_login_initializations", lambda *_args: 0)
    monkeypatch.setattr(worker, "drain_account_login_reconciliation", lambda *_args: 2)
    monkeypatch.setattr(worker, "drain_notification_outbox", lambda *_args: 3)
    monkeypatch.setattr(
        worker,
        "drain_account_login_batches",
        lambda *_args: calls.__setitem__("batch", calls["batch"] + 1) or 7,
    )

    assert worker._drain_account_login_once(4) == expected
    assert calls["batch"] == batch_calls


def test_compose_and_release_script_mount_dedicated_account_login_worker() -> None:
    compose = (PROJECT_ROOT / "docker-compose.server.yml").read_text()
    release_script = (PROJECT_ROOT / "deploy/compose-up.sh").read_text()

    assert "worker-account-login:" in compose
    assert '"--role", "account-login"' in compose
    assert "WORKER_ROLE: account-login" in compose
    assert "ACCOUNT_BATCH_LOGIN_WORKER_CONCURRENCY" in compose
    assert "ACCOUNT_POST_LOGIN_INIT_MODE" in compose
    account_login_service = compose.split("worker-account-login:", 1)[1].split(
        "worker-material-cache:", 1
    )[0]
    assert "logging: *default-logging" in account_login_service
    assert '"${ACCOUNT_POST_LOGIN_INIT_MODE:-off}" != "off"' in release_script
    assert "WORKER_SERVICES+=(worker-account-login)" in release_script
