from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import TgAccount, TgAccountLoginBatchItem
from app.services.account_login import host_rate_policy
from app.services.account_login.batches import create_login_batch
from app.services.account_login.contracts import BatchLoginError, LoginMaterials
from app.services.code_source_client import CodeSourceClient, HttpResult, parse_login_materials_html
from tests.test_account_batch_login_core import (
    _SuccessfulTwoFaGateway,
    _configure_drain_runtime,
    _create_payload,
    session_factory,
)


pytestmark = pytest.mark.no_postgres
CONFIG2_LINE = "+12025550125|https://api.config2.top/tgapi/tgapi/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee/GetHTML"


class _EmptyBaselineLoginCodeClient:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_login_materials(self, _url: str) -> LoginMaterials:
        self.calls += 1
        if self.calls == 1:
            return LoginMaterials("", "", "", "")
        return LoginMaterials("22222", "two-fa-secret", "new-time", "new-fetch")


class _MissingNumberTransport:
    def get(self, _url: str) -> HttpResult:
        return HttpResult(
            status=200,
            content_type="text/html; charset=utf-8",
            content_encoding="",
            body="<title>错误</title><p>此号不存在</p>".encode(),
        )


class _HtmlTransport:
    def __init__(self, message: str) -> None:
        self.message = message

    def get(self, _url: str) -> HttpResult:
        return HttpResult(
            status=200,
            content_type="text/html; charset=utf-8",
            content_encoding="",
            body=f"<title>错误</title><p>{self.message}</p>".encode(),
        )


def test_config2_client_routes_missing_number_to_empty_baseline() -> None:
    url = CONFIG2_LINE.split("|", 1)[1]

    materials = CodeSourceClient(transport=_MissingNumberTransport()).fetch_login_materials(url)

    assert materials == LoginMaterials("", "", "", "")


def test_config2_client_routes_no_recent_login_message_to_empty_baseline() -> None:
    url = CONFIG2_LINE.split("|", 1)[1]
    transport = _HtmlTransport("无三十分钟内的登录消息")

    materials = CodeSourceClient(transport=transport).fetch_login_materials(url)

    assert materials == LoginMaterials("", "", "", "")


def test_no_recent_login_message_stays_error_for_other_sources() -> None:
    html = "<title>错误</title><p>无三十分钟内的登录消息</p>"

    with pytest.raises(BatchLoginError, match="接码平台报告凭据无效") as exc_info:
        parse_login_materials_html(html, source_host="tgbotchecker")

    assert exc_info.value.code == "url_error"


def test_unknown_config2_error_stays_error() -> None:
    html = "<title>错误</title><p>未知供应方错误</p>"

    with pytest.raises(BatchLoginError, match="接码平台报告凭据无效") as exc_info:
        parse_login_materials_html(html, source_host="config2")

    assert exc_info.value.code == "url_error"


def test_config2_uses_dedicated_host_rate_policy() -> None:
    assert host_rate_policy.host_rate_policy("config2", 3) == ("config2", 130)
    assert host_rate_policy.host_rate_policy("susubot", 3) == ("susubot", 3)


def test_config2_empty_baseline_continues_to_real_login(session_factory, monkeypatch) -> None:
    monkeypatch.setattr(host_rate_policy, "CONFIG2_MIN_REQUEST_INTERVAL_SECONDS", 0)
    with session_factory() as session:
        batch = create_login_batch(
            session,
            1,
            20,
            "测试操作员",
            _create_payload(session, CONFIG2_LINE, key="config2-empty-baseline-full-flow"),
        )
    login_gateway = _SuccessfulTwoFaGateway()
    drain = _configure_drain_runtime(monkeypatch, login_gateway)
    code_client = _EmptyBaselineLoginCodeClient()

    for _ in range(12):
        drain.drain_account_login_batches(session_factory, 1, code_client=code_client)

    with session_factory() as session:
        item = session.scalar(select(TgAccountLoginBatchItem).where(TgAccountLoginBatchItem.batch_id == batch.id))
        account = session.get(TgAccount, item.account_id)

    assert item.status == "post_initialization_waiting"
    assert item.authorization_status == "confirmed"
    assert account.status == "在线"
    assert account.code_source_note == "config2 · aaaaaaaa…eeee"
    assert login_gateway.start_calls == 1
    assert login_gateway.finish_calls == [("22222", None), (None, "two-fa-secret")]
