from __future__ import annotations

import hashlib
import hmac
import re
from urllib.parse import parse_qsl, urlsplit

from app.security import get_token_key
from app.services._common import mask_phone

from .contracts import BatchLoginError, CodeSourceSpec, ParsedLoginLine


CODE_SOURCE_HOST = "tgbotchecker.com"
CODE_SOURCE_LABEL = "tgbotchecker"
CODE_SOURCE_PATH = "/GetHTML"
SUSUBOT_CODE_SOURCE_HOST = "tgapi.susubot.com"
SUSUBOT_CODE_SOURCE_LABEL = "susubot"
SUSUBOT_CODE_SOURCE_PATH = "/index.html"
SUSUBOT_CODE_SOURCE_TYPE = "107"
SUPPORTED_CODE_SOURCE_HOSTS = (CODE_SOURCE_HOST, SUSUBOT_CODE_SOURCE_HOST)
UUID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
API_KEY_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")


def parse_code_source_url(url: str) -> CodeSourceSpec:
    raw = (url or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise BatchLoginError("url_domain_not_allowed", "接码地址格式错误") from exc
    if parsed.scheme != "https" or parsed.hostname not in SUPPORTED_CODE_SOURCE_HOSTS:
        raise BatchLoginError("url_domain_not_allowed", "仅支持指定的 HTTPS 接码平台")
    if parsed.username or parsed.password or parsed.fragment:
        raise BatchLoginError("url_domain_not_allowed", "接码地址路径或凭据不符合要求")
    if port not in {None, 443}:
        raise BatchLoginError("url_domain_not_allowed", "接码地址仅允许 443 端口")
    if parsed.hostname == SUSUBOT_CODE_SOURCE_HOST:
        return _parse_susubot_url(parsed.path, parsed.query)
    return _parse_tgbotchecker_url(parsed.path, parsed.query)


def _parse_tgbotchecker_url(path: str, query: str) -> CodeSourceSpec:
    if path != CODE_SOURCE_PATH:
        raise BatchLoginError("url_domain_not_allowed", "接码地址路径或凭据不符合要求")
    if not query.startswith("uuid=") or not UUID_RE.fullmatch(query.removeprefix("uuid=")):
        raise BatchLoginError("url_domain_not_allowed", "接码地址必须包含唯一的 32 位 UUID")
    uuid_value = query.removeprefix("uuid=").lower()
    canonical = f"https://{CODE_SOURCE_HOST}{CODE_SOURCE_PATH}?uuid={uuid_value}"
    return _code_source_spec(canonical, CODE_SOURCE_LABEL, CODE_SOURCE_HOST, uuid_value, 6)


def _parse_susubot_url(path: str, query: str) -> CodeSourceSpec:
    if path != SUSUBOT_CODE_SOURCE_PATH:
        raise BatchLoginError("url_domain_not_allowed", "接码地址路径或凭据不符合要求")
    try:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise BatchLoginError("url_domain_not_allowed", "接码地址参数格式错误") from exc
    params = dict(pairs)
    if len(pairs) != len(params):
        raise BatchLoginError("url_domain_not_allowed", "接码地址参数重复")
    if set(params) != {"type", "apikey"} or params["type"] != SUSUBOT_CODE_SOURCE_TYPE:
        raise BatchLoginError("url_domain_not_allowed", "接码地址必须包含固定 type 与 apikey")
    api_key = params["apikey"].lower()
    if not API_KEY_RE.fullmatch(api_key):
        raise BatchLoginError("url_domain_not_allowed", "接码地址必须包含有效 apikey")
    canonical = f"https://{SUSUBOT_CODE_SOURCE_HOST}{SUSUBOT_CODE_SOURCE_PATH}?type={SUSUBOT_CODE_SOURCE_TYPE}&apikey={api_key}"
    return _code_source_spec(canonical, SUSUBOT_CODE_SOURCE_LABEL, SUSUBOT_CODE_SOURCE_HOST, api_key, 8)


def _code_source_spec(canonical: str, label: str, host: str, value: str, prefix_len: int) -> CodeSourceSpec:
    return CodeSourceSpec(
        url=canonical,
        host=label,
        uuid=value,
        uuid_fingerprint=hashlib.sha256(f"{host}:{value}".encode()).hexdigest(),
        uuid_hint=f"{value[:prefix_len]}…{value[-4:]}",
    )


def normalize_phone(phone: str) -> str:
    normalized = re.sub(r"[\s()-]", "", (phone or "").strip())
    if not PHONE_RE.fullmatch(normalized):
        raise BatchLoginError("line_format_invalid", "手机号必须是带国家码的 E.164 格式")
    return normalized


def parse_login_lines(lines_text: str, *, max_lines: int) -> list[ParsedLoginLine]:
    raw_lines = [value.strip() for value in (lines_text or "").splitlines() if value.strip()]
    if not raw_lines:
        raise BatchLoginError("line_format_invalid", "至少需要一行账号")
    if len(raw_lines) > max_lines:
        raise BatchLoginError("quota_exceeded", f"一次最多提交 {max_lines} 行")
    parsed = [_parse_login_line(value, index) for index, value in enumerate(raw_lines, start=1)]
    _reject_duplicate_lines(parsed)
    return parsed


def _parse_login_line(value: str, line_no: int) -> ParsedLoginLine:
    if value.count("|") != 1:
        raise BatchLoginError("line_format_invalid", "每行必须是 手机号|接码地址", line_no=line_no)
    phone_raw, url = value.split("|", 1)
    try:
        phone = normalize_phone(phone_raw)
        source = parse_code_source_url(_strip_url_backticks(url))
    except BatchLoginError as exc:
        raise BatchLoginError(exc.code, str(exc), line_no=line_no) from exc
    return ParsedLoginLine(line_no, phone, mask_phone(phone), source)


def _strip_url_backticks(value: str) -> str:
    url = value.strip()
    if url.startswith("`") and url.endswith("`") and len(url) >= 2:
        return url[1:-1].strip()
    return url


def _reject_duplicate_lines(lines: list[ParsedLoginLine]) -> None:
    phones: set[str] = set()
    uuids: dict[str, str] = {}
    for line in lines:
        if line.phone in phones:
            raise BatchLoginError("line_format_invalid", "批次内手机号重复", line_no=line.line_no)
        phones.add(line.phone)
        previous_phone = uuids.get(line.source.uuid_fingerprint)
        if previous_phone and previous_phone != line.phone:
            raise BatchLoginError("code_source_binding_conflict", "同一 UUID 不能对应多个手机号", line_no=line.line_no)
        uuids[line.source.uuid_fingerprint] = line.phone


def phone_fingerprint(tenant_id: int, phone: str, key_version: int) -> str:
    message = f"account-batch-phone:v{key_version}:{tenant_id}:{phone}".encode()
    return hmac.new(get_token_key(), message, hashlib.sha256).hexdigest()


def phone_fingerprints(tenant_id: int, phone: str, versions: tuple[int, ...]) -> dict[int, str]:
    return {version: phone_fingerprint(tenant_id, phone, version) for version in versions}


def material_hmac(value: str) -> str:
    return hmac.new(get_token_key(), f"account-batch-material:{value}".encode(), hashlib.sha256).hexdigest()


__all__ = [
    "CODE_SOURCE_HOST",
    "SUPPORTED_CODE_SOURCE_HOSTS",
    "SUSUBOT_CODE_SOURCE_HOST",
    "material_hmac",
    "normalize_phone",
    "parse_code_source_url",
    "parse_login_lines",
    "phone_fingerprint",
    "phone_fingerprints",
]
