from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from app.config import Settings
from app.integrations.telegram import DeveloperAppCredentials
from app.integrations.telegram.gateway import TelethonTelegramGateway
from app.security import encrypt_session


pytestmark = pytest.mark.no_postgres


@dataclass
class FakeButton:
    text: str
    effect: str
    url: str = ""


class FakeMessage:
    def __init__(self, text: str, rows: list[list[FakeButton]]) -> None:
        self.message = text
        self.buttons = rows
        self.clicks: list[tuple[int, int]] = []
        self.raise_on_click = False

    async def click(self, row: int, col: int) -> None:
        self.clicks.append((row, col))
        if self.raise_on_click:
            raise RuntimeError("transport closed after click")


class FakeConversation:
    def __init__(self, pages: list[FakeMessage]) -> None:
        self.pages = pages
        self.sent: list[str] = []
        self.index = 0

    async def __aenter__(self) -> "FakeConversation":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def send_message(self, text: str) -> None:
        self.sent.append(text)

    async def get_response(self) -> FakeMessage:
        item = self.pages[self.index]
        self.index += 1
        return item


class FakeClient:
    def __init__(self, pages: list[FakeMessage]) -> None:
        self.pages = pages

    def conversation(self, _bot: str, timeout: int = 60) -> FakeConversation:
        return FakeConversation(self.pages)


def _payload() -> dict[str, Any]:
    return {
        "bot_username": "jisou",
        "target_group_ids": [1001],
        "exempt_group_username": "exempt_group",
        "runtime_environment": {
            "proxy_egress_guard": "verified",
            "group_proxy_binding_id": "1",
            "runtime_proxy_id": "31",
            "binding_generation": "1",
            "observed_exit_ip": "8.8.8.8",
        },
    }


def _credentials() -> DeveloperAppCredentials:
    return DeveloperAppCredentials(
        app_id=1,
        api_id=123,
        api_hash="hash",
        credentials_version=1,
        proxy_id=31,
        proxy_protocol="socks5",
        proxy_host="127.0.0.1",
        proxy_port=1080,
    )


def test_telethon_gateway_exposes_rank_deboost_methods() -> None:
    gateway = TelethonTelegramGateway(Settings(telethon_operation_timeout_seconds=1))

    assert callable(getattr(gateway, "search_rank_deboost_candidates", None))
    assert callable(getattr(gateway, "execute_search_rank_deboost", None))


def test_rank_deboost_executor_clicks_one_safe_navigation_button() -> None:
    from app.integrations.telegram.search_rank_deboost import execute_rank_deboost_with_client

    page = FakeMessage(
        "1. @competitor_a\n2. @my_target\n3. @competitor_b",
        [[
            FakeButton("详情A", "navigate_only"),
            FakeButton("加入", "join_candidate", "https://t.me/competitor_a"),
            FakeButton("外部", "external_http_url", "https://example.com"),
            FakeButton("未知", "unknown"),
        ]],
    )
    client = FakeClient([FakeMessage("start", []), page])

    result = asyncio.run(execute_rank_deboost_with_client(client, _payload(), keyword_text="keyword"))

    assert result["success"] is True
    assert result["execution_status"] == "confirmed"
    assert page.clicks == [(0, 0)]
    assert result["click_outcomes"] == [{"row": 0, "col": 0, "effect": "navigate_only", "status": "confirmed"}]
    assert [item["position"] for item in result["search_results"]] == [1, 2, 3]


def test_rank_deboost_executor_returns_unknown_after_ambiguous_click() -> None:
    from app.integrations.telegram.search_rank_deboost import execute_rank_deboost_with_client

    page = FakeMessage("1. @competitor_a\n2. @my_target", [[FakeButton("详情A", "navigate_only")]])
    page.raise_on_click = True
    client = FakeClient([FakeMessage("start", []), page])

    result = asyncio.run(execute_rank_deboost_with_client(client, _payload(), keyword_text="keyword"))

    assert result["success"] is False
    assert result["execution_status"] == "unknown_after_click"
    assert page.clicks == [(0, 0)]
    assert result["click_outcomes"][0]["status"] == "unknown_after_click"


def test_gateway_does_not_create_client_when_proxy_egress_probe_fails() -> None:
    class ProbeFailGateway(TelethonTelegramGateway):
        async def _probe_rank_deboost_proxy_egress_async(self, credentials, expected_exit_ip):
            return ""

        async def _get_or_create_client(self, *_args, **_kwargs):
            raise AssertionError("client must not be created after failed egress proof")

    gateway = ProbeFailGateway(Settings(telethon_operation_timeout_seconds=1))

    result = asyncio.run(
        gateway._execute_search_rank_deboost_async(
            encrypt_session("raw-session"),
            _credentials(),
            _payload(),
            "keyword",
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "proxy_egress_guard_failed"
