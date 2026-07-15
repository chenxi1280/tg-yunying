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
