import pytest

from app.config import Settings


pytestmark = pytest.mark.no_postgres


def test_production_rejects_default_bootstrap_admin_password() -> None:
    with pytest.raises(ValueError, match="ADMIN_BOOTSTRAP_PASSWORD"):
        Settings(
            app_env="production",
            session_secret_key="secure-session-secret",
            admin_bootstrap_password="admin123",
        )


def test_development_allows_default_bootstrap_admin_password() -> None:
    settings = Settings(app_env="development", admin_bootstrap_password="admin123")

    assert settings.admin_bootstrap_password == "admin123"


def test_account_online_probe_concurrency_must_be_positive() -> None:
    with pytest.raises(ValueError, match="ACCOUNT_ONLINE_PROBE_CONCURRENCY"):
        Settings(account_online_probe_concurrency=0)


def test_account_online_probe_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="ACCOUNT_ONLINE_PROBE_TIMEOUT_SECONDS"):
        Settings(account_online_probe_timeout_seconds=0)


def test_verification_contract_requires_dispatcher_recycle() -> None:
    with pytest.raises(ValueError, match="DISPATCHER_RECYCLE_ENABLED"):
        Settings(
            image_verification_contract_enabled=True,
            dispatcher_recycle_enabled=False,
        )


def test_production_verification_contract_requires_remote_ocr() -> None:
    with pytest.raises(ValueError, match="IMAGE_VERIFICATION_OCR_BACKEND=remote"):
        Settings(
            app_env="production",
            session_secret_key="secure-session-secret",
            admin_bootstrap_password="secure-admin-password",
            image_verification_contract_enabled=True,
            image_verification_ocr_backend="local",
            dispatcher_recycle_enabled=True,
            dispatcher_recycle_soft_rss_bytes=1,
            dispatcher_recycle_lease_seconds=60,
            dispatcher_gateway_shutdown_timeout_seconds=15,
        )
