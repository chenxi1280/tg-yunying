from __future__ import annotations

import json
import gzip
import http.client
import ipaddress
import socket
import ssl
import time
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urlsplit

from app.services.account_login.contracts import BatchLoginError, LoginMaterials
from app.services.account_login.identity import (
    CODE_SOURCE_HOST,
    SUPPORTED_CODE_SOURCE_HOSTS,
    SUSUBOT_CODE_SOURCE_HOST,
    parse_code_source_url,
)


MAX_RESPONSE_BYTES = 256 * 1024
REQUEST_TIMEOUT_SECONDS = 15
USER_AGENT = "tg-yunying-login-worker/1.0"
RETRY_DELAYS_SECONDS = (0, 1, 3)
READINESS_CACHE_SECONDS = 60
_readiness_cache: dict[tuple[str, ...], tuple[float, str]] = {}


@dataclass(frozen=True)
class HttpResult:
    status: int
    content_type: str
    content_encoding: str
    body: bytes


class _LoginMaterialParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.text: list[str] = []
        self.inputs: dict[str, str] = {}
        self.values: dict[str, str] = {}
        self._in_title = False
        self._pending_time_key = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "title":
            self._in_title = True
        if tag == "input" and values.get("id"):
            self.inputs[values["id"]] = values.get("value", "")
        element_id = values.get("id", "")
        if element_id in {"login_time", "last_fetch_time"}:
            self.values[element_id] = values.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if not value:
            return
        self.text.append(value)
        if self._in_title:
            self.title += value
        if self._pending_time_key and value not in {":", "："}:
            self.values[self._pending_time_key] = value
            self._pending_time_key = ""
            return
        for label, key in (("登录时间", "login_time"), ("上次获取时间", "last_fetch_time")):
            if not value.startswith(label):
                continue
            remainder = value[len(label):].lstrip(" :：")
            if remainder:
                self.values[key] = remainder
            else:
                self._pending_time_key = key
            return


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, pinned_ip: str, *, timeout: int) -> None:
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self.pinned_ip = pinned_ip

    def connect(self) -> None:
        raw_socket = socket.create_connection((self.pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        peer_ip = str(self.sock.getpeername()[0])
        if peer_ip != self.pinned_ip:
            self.sock.close()
            raise BatchLoginError("url_ssrf_rejected", "接码平台连接地址校验失败")


class PinnedHttpsTransport:
    def get(self, url: str) -> HttpResult:
        spec = parse_code_source_url(url)
        host, path = _request_target(spec.url)
        pinned_ip = _resolve_public_ip(host)
        connection = _PinnedHTTPSConnection(host, pinned_ip, timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            connection.request(
                "GET", path,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html, application/json",
                    "Accept-Encoding": "identity",
                },
            )
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise BatchLoginError("url_fetch_failed", "接码平台响应超过大小限制")
            return HttpResult(
                status=response.status,
                content_type=response.getheader("Content-Type", ""),
                content_encoding=response.getheader("Content-Encoding", ""),
                body=body,
            )
        finally:
            connection.close()


class CodeSourceClient:
    def __init__(self, transport: PinnedHttpsTransport | None = None, sleeper: Callable[[float], None] = time.sleep) -> None:
        self.transport = transport or PinnedHttpsTransport()
        self.sleeper = sleeper

    def fetch_login_materials(self, url: str) -> LoginMaterials:
        parse_code_source_url(url)
        last_error: Exception | None = None
        for delay in RETRY_DELAYS_SECONDS:
            if delay:
                self.sleeper(delay)
            try:
                result = self.transport.get(url)
                return parse_login_materials_response(result)
            except BatchLoginError as exc:
                if exc.code != "url_fetch_failed":
                    raise
                last_error = exc
            except (OSError, TimeoutError, ssl.SSLError, http.client.HTTPException) as exc:
                last_error = exc
        raise BatchLoginError("url_fetch_failed", "接码平台请求失败") from last_error


def code_source_readiness(hosts: tuple[str, ...] = SUPPORTED_CODE_SOURCE_HOSTS) -> str:
    now = time.monotonic()
    cache_key = tuple(sorted(set(hosts)))
    cached = _readiness_cache.get(cache_key)
    if cached and now - cached[0] < READINESS_CACHE_SECONDS:
        return cached[1]
    blocker = ""
    for host in cache_key:
        blocker = _readiness_for_host(host)
        if blocker:
            break
    _readiness_cache[cache_key] = (now, blocker)
    return blocker


def _readiness_for_host(host: str) -> str:
    try:
        result = PinnedHttpsTransport().get(_readiness_url(host))
        if result.status != 200 or not _readiness_content_type_ok(host, result.content_type):
            return "code_source_https_unready"
    except BatchLoginError as exc:
        return exc.code
    except Exception:
        return "code_source_https_unready"
    return ""


def _readiness_url(host: str) -> str:
    if host == SUSUBOT_CODE_SOURCE_HOST:
        return f"https://{host}/index.html?type=107&apikey=00000000-0000-0000-0000-000000000000"
    return f"https://{CODE_SOURCE_HOST}/GetHTML?uuid=00000000000000000000000000000000"


def _readiness_content_type_ok(host: str, content_type: str) -> bool:
    if host == SUSUBOT_CODE_SOURCE_HOST:
        return content_type.lower().startswith("application/json")
    return content_type.lower().startswith("text/html")


def _request_target(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if host == SUSUBOT_CODE_SOURCE_HOST:
        return host, f"/api/code?{parsed.query}"
    return host, f"{parsed.path}?{parsed.query}"


def _resolve_public_ip(host: str) -> str:
    try:
        addresses = {result[4][0] for result in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise BatchLoginError("url_fetch_failed", "接码平台 DNS 解析失败") from exc
    if not addresses or any(not _is_public_ip(value) for value in addresses):
        raise BatchLoginError("url_ssrf_rejected", "接码平台解析到非公网地址")
    return sorted(addresses)[0]


def _is_public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    benchmark = ipaddress.ip_network("198.18.0.0/15")
    return address.is_global and address not in benchmark


def parse_login_materials_response(result: HttpResult) -> LoginMaterials:
    if result.status != 200:
        raise BatchLoginError("url_fetch_failed", f"接码平台返回 HTTP {result.status}")
    content_type = result.content_type.lower()
    if content_type.startswith("application/json"):
        return parse_login_materials_json(_decode_response_body(result))
    if not content_type.startswith("text/html"):
        raise BatchLoginError("url_fetch_failed", "接码平台返回了非 HTML 内容")
    body = _decode_response_body(result)
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BatchLoginError("url_parse_failed", "接码平台页面编码无法解析") from exc
    return parse_login_materials_html(html)


def _decode_response_body(result: HttpResult) -> bytes:
    return _decompress(result.body, result.content_encoding)


def _decompress(body: bytes, encoding: str) -> bytes:
    try:
        if encoding.lower() == "gzip":
            body = gzip.decompress(body)
        elif encoding.lower() == "deflate":
            body = zlib.decompress(body)
    except (OSError, zlib.error) as exc:
        raise BatchLoginError("url_parse_failed", "接码平台响应解压失败") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise BatchLoginError("url_fetch_failed", "接码平台解压响应超过大小限制")
    return body


def parse_login_materials_html(html: str) -> LoginMaterials:
    parser = _LoginMaterialParser()
    try:
        parser.feed(html)
    except Exception as exc:
        raise BatchLoginError("url_parse_failed", "接码平台页面解析失败") from exc
    body_text = " ".join(parser.text)
    if "错误" in parser.title or "此号不存在" in body_text:
        raise BatchLoginError("url_error", "接码平台报告凭据无效")
    if "code" not in parser.inputs:
        raise BatchLoginError("url_parse_failed", "接码平台页面缺少验证码字段")
    return LoginMaterials(
        code=parser.inputs["code"].strip(),
        password_2fa=parser.inputs.get("pass2fa", "").strip(),
        login_time=parser.values.get("login_time", parser.inputs.get("login_time", "")).strip(),
        last_fetch_time=parser.values.get("last_fetch_time", parser.inputs.get("last_fetch_time", "")).strip(),
    )


def parse_login_materials_json(body: bytes) -> LoginMaterials:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchLoginError("url_parse_failed", "接码平台 JSON 无法解析") from exc
    if payload.get("status") == -1:
        raise BatchLoginError("url_fetch_failed", "接码平台请求频繁")
    if payload.get("status") != 1:
        raise BatchLoginError("url_error", "接码平台报告凭据无效")
    code = str(payload.get("msg", "")).strip()
    if not code:
        raise BatchLoginError("url_parse_failed", "接码平台 JSON 缺少验证码")
    return LoginMaterials(
        code=code,
        password_2fa=str(payload.get("2fa", "")).strip(),
        login_time="",
        last_fetch_time="",
    )


__all__ = [
    "CodeSourceClient",
    "HttpResult",
    "code_source_readiness",
    "parse_login_materials_html",
    "parse_login_materials_json",
    "parse_login_materials_response",
]
