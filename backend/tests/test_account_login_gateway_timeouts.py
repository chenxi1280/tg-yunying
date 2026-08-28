from __future__ import annotations

import pytest

from app.config import Settings
from app.integrations.telegram import DeveloperAppCredentials, TelethonTelegramGateway
from app.integrations.telegram.gateway import ACCOUNT_LOGIN_OPERATION_TIMEOUT_SECONDS
from app.services.account_login.state import LEASE_SECONDS


pytestmark = pytest.mark.no_postgres


class _RunCapture:
    def __init__(self) -> None:
        self.timeouts: list[float | None] = []

    def run(self, coroutine, *, timeout_seconds=None):
        coroutine.close()
        self.timeouts.append(timeout_seconds)
        return "captured"


def _gateway() -> tuple[TelethonTelegramGateway, _RunCapture]:
    gateway = TelethonTelegramGateway(Settings())
    capture = _RunCapture()
    gateway._lifecycle = capture
    return gateway, capture


def _credentials() -> DeveloperAppCredentials:
    return DeveloperAppCredentials(
        app_id=1,
        api_id=123,
        api_hash="hash",
        credentials_version=1,
    )


def test_code_login_start_timeout_is_shorter_than_claim_lease() -> None:
    gateway, capture = _gateway()

    result = gateway.start_login(
        "code",
        flow_id=1,
        account_id=1,
        phone="+10000000000",
        credentials=_credentials(),
    )

    assert result == "captured"
    assert capture.timeouts == [ACCOUNT_LOGIN_OPERATION_TIMEOUT_SECONDS]
    assert ACCOUNT_LOGIN_OPERATION_TIMEOUT_SECONDS < LEASE_SECONDS


def test_code_login_finish_uses_same_bounded_timeout() -> None:
    gateway, capture = _gateway()

    result = gateway.finish_login(
        "12345",
        None,
        flow_id=1,
        account_id=1,
        phone="+10000000000",
        credentials=_credentials(),
        temporary_session="session",
        phone_code_hash="hash",
    )

    assert result == "captured"
    assert capture.timeouts == [ACCOUNT_LOGIN_OPERATION_TIMEOUT_SECONDS]
