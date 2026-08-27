from __future__ import annotations

import pytest

from app.auth import ROLE_TEMPLATE_PERMISSIONS, all_permissions
from app.models import TgAccount
from app.permission_middleware import permission_check_result, required_permission
from app.services.account_login.contracts import BatchLoginError
from app.services.account_login.identity import parse_code_source_url, parse_login_lines
from app.services.code_source_client import (
    HttpResult,
    _readiness_url,
    _request_target,
    parse_login_materials_html,
    parse_login_materials_json,
    parse_login_materials_response,
)


pytestmark = pytest.mark.no_postgres


VALID_UUID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
VALID_URL = f"https://tgbotchecker.com/GetHTML?uuid={VALID_UUID}"
SUSUBOT_API_KEY = "11111111-2222-4333-8444-555555555555"
SUSUBOT_URL = f"https://tgapi.susubot.com/index.html?type=107&apikey={SUSUBOT_API_KEY}"
CONFIG2_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CONFIG2_URL = f"https://api.config2.top/tgapi/tgapi/{CONFIG2_UUID}/GetHTML"


def test_parse_login_lines_preserves_phone_uuid_mapping() -> None:
    lines = parse_login_lines(f"+12025550123|{VALID_URL}", max_lines=100)

    assert len(lines) == 1
    assert lines[0].phone == "+12025550123"
    assert lines[0].source.uuid == VALID_UUID
    assert lines[0].source.uuid_hint == "a1b2c3…8f90"
    assert VALID_UUID not in lines[0].phone_masked


def test_parse_login_lines_accepts_paired_markdown_backticks() -> None:
    lines = parse_login_lines(f"+12025550123|`{VALID_URL}`", max_lines=100)

    assert lines[0].source.url == VALID_URL


def test_parse_login_lines_accepts_susubot_https_platform() -> None:
    lines = parse_login_lines(f"+12025550123|{SUSUBOT_URL}", max_lines=100)

    assert lines[0].source.url == SUSUBOT_URL
    assert lines[0].source.host == "susubot"
    assert lines[0].source.uuid == SUSUBOT_API_KEY
    assert lines[0].source.uuid_hint == "11111111…5555"
    assert SUSUBOT_API_KEY not in lines[0].phone_masked


def test_parse_login_lines_accepts_config2_https_platform() -> None:
    lines = parse_login_lines(f"+12025550125|{CONFIG2_URL}", max_lines=100)

    assert lines[0].source.url == CONFIG2_URL
    assert lines[0].source.host == "config2"
    assert lines[0].source.uuid == CONFIG2_UUID
    assert lines[0].source.uuid_hint == "aaaaaaaa…eeee"
    assert CONFIG2_UUID not in lines[0].phone_masked


def test_config2_transport_keeps_exact_path_without_empty_query() -> None:
    assert _request_target(CONFIG2_URL) == (
        "api.config2.top",
        f"/tgapi/tgapi/{CONFIG2_UUID}/GetHTML",
    )
    assert _readiness_url("api.config2.top").endswith(
        "/tgapi/tgapi/00000000-0000-0000-0000-000000000000/GetHTML"
    )


@pytest.mark.parametrize(
    "url",
    [
        f"http://tgbotchecker.com/GetHTML?uuid={VALID_UUID}",
        f"https://user@tgbotchecker.com/GetHTML?uuid={VALID_UUID}",
        f"https://tgbotchecker.com:444/GetHTML?uuid={VALID_UUID}",
        f"https://tgbotchecker.com/GetHTML?uuid={VALID_UUID}&extra=1",
        f"https://tgbotchecker.com/GetHTML?uuid=%61{VALID_UUID[1:]}",
        f"https://tgbotchecker.com/GetHTML?uuid={VALID_UUID}#fragment",
    ],
)
def test_code_source_url_rejects_non_exact_urls(url: str) -> None:
    with pytest.raises(BatchLoginError) as error:
        parse_code_source_url(url)

    assert error.value.code == "url_domain_not_allowed"
    assert VALID_UUID not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        f"http://tgapi.susubot.com/index.html?type=107&apikey={SUSUBOT_API_KEY}",
        f"https://tgapi.susubot.com/get.html?type=107&apikey={SUSUBOT_API_KEY}",
        f"https://tgapi.susubot.com/index.html?type=108&apikey={SUSUBOT_API_KEY}",
        f"https://tgapi.susubot.com/index.html?type=107&apikey={SUSUBOT_API_KEY}&extra=1",
        f"https://tgapi.susubot.com/index.html?type=107&apikey=not-a-key",
        f"https://tgapi.susubot.com/index.html?type=107&apikey={SUSUBOT_API_KEY}#fragment",
    ],
)
def test_susubot_url_rejects_non_exact_urls(url: str) -> None:
    with pytest.raises(BatchLoginError) as error:
        parse_code_source_url(url)

    assert error.value.code == "url_domain_not_allowed"
    assert SUSUBOT_API_KEY not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        f"http://api.config2.top/tgapi/tgapi/{CONFIG2_UUID}/GetHTML",
        f"https://api.config2.top/GetHTML?uuid={CONFIG2_UUID}",
        f"https://api.config2.top/tgapi/tgapi/{CONFIG2_UUID}/GetHTML?extra=1",
        f"https://api.config2.top/tgapi/tgapi/not-a-uuid/GetHTML",
        f"https://api.config2.top/tgapi/tgapi/{CONFIG2_UUID}/GetHTML#fragment",
    ],
)
def test_config2_url_rejects_non_exact_urls(url: str) -> None:
    with pytest.raises(BatchLoginError) as error:
        parse_code_source_url(url)

    assert error.value.code == "url_domain_not_allowed"
    assert CONFIG2_UUID not in str(error.value)


def test_parse_login_lines_rejects_uuid_reuse_for_other_phone() -> None:
    text = f"+12025550123|{VALID_URL}\n+12025550124|{VALID_URL}"

    with pytest.raises(BatchLoginError) as error:
        parse_login_lines(text, max_lines=100)

    assert error.value.code == "code_source_binding_conflict"
    assert error.value.line_no == 2


def test_parse_login_lines_rejects_susubot_key_reuse_for_other_phone() -> None:
    text = f"+12025550123|{SUSUBOT_URL}\n+12025550124|{SUSUBOT_URL}"

    with pytest.raises(BatchLoginError) as error:
        parse_login_lines(text, max_lines=100)

    assert error.value.code == "code_source_binding_conflict"
    assert error.value.line_no == 2


def test_parse_login_lines_rejects_config2_key_reuse_for_other_phone() -> None:
    text = f"+12025550123|{CONFIG2_URL}\n+12025550124|{CONFIG2_URL}"

    with pytest.raises(BatchLoginError) as error:
        parse_login_lines(text, max_lines=100)

    assert error.value.code == "code_source_binding_conflict"
    assert error.value.line_no == 2


def test_material_parser_recognizes_html_rate_limit() -> None:
    html = """
    <html><head><title>错误 - Telegram 登录接码工具</title></head><body>
      <div class="error-message"><h3>错误信息:</h3><p>请求过于频繁，请等待 66秒再试。</p></div>
    </body></html>
    """

    with pytest.raises(BatchLoginError) as error:
        parse_login_materials_html(html)

    assert error.value.code == "url_fetch_failed"
    assert "请求频繁" in str(error.value)


def test_material_parser_uses_input_ids_not_attribute_order() -> None:
    html = """
    <html><head><title>Telegram 登录接码工具</title></head><body>
      <input value="12345" class="field" id="code">
      <input value="safe-temporary-password" id="pass2fa" type="text">
      <input id="login_time" value="页面登录时间原文">
      <input value="页面刷新时间原文" id="last_fetch_time">
    </body></html>
    """

    materials = parse_login_materials_html(html)

    assert materials.code == "12345"
    assert materials.password_2fa == "safe-temporary-password"
    assert materials.login_time == "页面登录时间原文"
    assert materials.last_fetch_time == "页面刷新时间原文"


def test_material_parser_rejects_http_200_error_page() -> None:
    result = HttpResult(
        status=200,
        content_type="text/html; charset=utf-8",
        content_encoding="",
        body="<title>错误 - Telegram 登录接码工具</title><p>此号不存在</p>".encode(),
    )

    with pytest.raises(BatchLoginError) as error:
        parse_login_materials_response(result)

    assert error.value.code == "url_error"


def test_material_parser_rejects_missing_code_field() -> None:
    with pytest.raises(BatchLoginError) as error:
        parse_login_materials_html("<html><title>Telegram 登录接码工具</title></html>")

    assert error.value.code == "url_parse_failed"


def test_material_parser_reads_susubot_json() -> None:
    materials = parse_login_materials_json(b'{"status":1,"msg":"12345","2fa":"safe-temporary-password"}')

    assert materials.code == "12345"
    assert materials.password_2fa == "safe-temporary-password"


def test_material_parser_routes_json_response() -> None:
    result = HttpResult(
        status=200,
        content_type="application/json; charset=utf-8",
        content_encoding="",
        body=b'{"status":1,"msg":"12345","2fa":""}',
    )

    materials = parse_login_materials_response(result)

    assert materials.code == "12345"


def test_material_parser_rejects_susubot_error_json() -> None:
    with pytest.raises(BatchLoginError) as error:
        parse_login_materials_json(b'{"btn":false,"status":0,"msg":"apikey error"}')

    assert error.value.code == "url_error"


def test_material_parser_reads_display_time_labels() -> None:
    html = """
    <title>Telegram 登录接码工具</title>
    <input id="code" value="12345"><input id="pass2fa" value="">
    <span>登录时间：</span><strong>页面登录时间</strong>
    <span>上次获取时间</span><strong>页面获取时间</strong>
    """

    materials = parse_login_materials_html(html)

    assert materials.login_time == "页面登录时间"
    assert materials.last_fetch_time == "页面获取时间"


def test_account_code_source_note_is_independent_from_display_name() -> None:
    account = TgAccount(
        tenant_id=1,
        display_name="可修改昵称",
        phone_masked="+120****0123",
        code_source_host="tgbotchecker",
        code_source_uuid_hint="a1b2c3…8f90",
    )

    assert account.code_source_note == "tgbotchecker · a1b2c3…8f90"
    account.display_name = "初始化后的昵称"
    assert account.code_source_note == "tgbotchecker · a1b2c3…8f90"


def test_batch_login_permissions_require_both_write_capabilities() -> None:
    permissions = all_permissions()
    specialist = ROLE_TEMPLATE_PERMISSIONS["账号添加专员"]
    required = required_permission("POST", "/api/tg-accounts/login-batches/precheck")

    assert "accounts.batch_login" in permissions
    assert "accounts.code_source_credentials.read" in permissions
    assert {"accounts.batch_login", "accounts.login", "accounts.code_source_credentials.read"} <= set(specialist)
    assert required == ("accounts.batch_login", "accounts.login")
    assert permission_check_result(required, {"accounts.batch_login"}) == ["accounts.login"]
    assert required_permission("GET", "/api/tg-accounts/login-batches/1") == ("accounts.view",)
    assert required_permission("POST", "/api/tg-accounts/1/code-source-binding/reveal") == (
        "accounts.view",
        "accounts.code_source_credentials.read",
    )
    assert permission_check_result(
        ("accounts.view", "accounts.code_source_credentials.read"),
        {"accounts.view"},
    ) == ["accounts.code_source_credentials.read"]
