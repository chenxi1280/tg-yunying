from __future__ import annotations

import hashlib
import ipaddress
import socket
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse
from uuid import uuid4

from app.config import get_settings
from app.storage import media_root

from .temp_files import TEMP_SUBDIRS, temp_dir

MEDIA_UPLOAD_TYPES = {"图片", "表情包", "文件"}
URL_MATERIAL_TYPES = {"图片", "表情包", "文件"}

MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/x-tgsticker": ".tgs",
    "video/webm": ".webm",
    "video/mp4": ".mp4",
    "application/pdf": ".pdf",
}

IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
STICKER_MIMES = IMAGE_MIMES | {"application/x-tgsticker", "video/webm"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - urllib hook.
        return None


def validate_material_url(url: str, *, material_type: str) -> str:
    value = url.strip()
    if not value:
        raise ValueError("素材 URL 不能为空")
    if len(value) > 2048:
        raise ValueError("素材 URL 过长")
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise ValueError("素材 URL 仅允许 https")
    if not parsed.netloc or not parsed.hostname:
        raise ValueError("素材 URL 缺少域名")
    host = parsed.hostname.strip().lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise ValueError("素材 URL 不允许指向 localhost")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise ValueError("素材 URL 不允许指向内网或本机地址")
    path = parsed.path.lower()
    if material_type in URL_MATERIAL_TYPES and path:
        allowed_suffixes = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tgs", ".webm", ".mp4", ".pdf"}
        if "." in Path(path).name and Path(path).suffix not in allowed_suffixes:
            raise ValueError("素材 URL 文件类型不支持")
    settings = get_settings()
    if settings.material_url_deep_probe_enabled:
        deep_probe_material_url(value, material_type=material_type)
    return value


def deep_probe_material_url(
    url: str,
    *,
    material_type: str,
    resolver=socket.getaddrinfo,
    opener=None,
) -> str:
    settings = get_settings()
    current = _basic_safe_url(url, material_type=material_type)
    http_opener = opener or urllib.request.build_opener(_NoRedirect)
    for _ in range(max(0, settings.material_url_probe_max_redirects) + 1):
        _assert_hostname_resolves_publicly(current, resolver=resolver)
        request = urllib.request.Request(current, method="HEAD", headers={"User-Agent": "tg-yunying-material-probe/1.0"})
        try:
            response = http_opener.open(request, timeout=settings.material_url_probe_timeout_seconds)
        except urllib.error.HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308} and exc.headers.get("Location"):
                current = _basic_safe_url(urljoin(current, exc.headers["Location"]), material_type=material_type)
                continue
            raise ValueError(f"素材 URL 探测失败：HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ValueError("素材 URL 探测失败") from exc
        with response:
            status = int(getattr(response, "status", 200) or 200)
            if status >= 400:
                raise ValueError(f"素材 URL 探测失败：HTTP {status}")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > settings.material_max_bytes:
                        raise ValueError(f"素材 URL 文件过大，最大 {settings.material_max_bytes} 字节")
                except ValueError as exc:
                    if "素材 URL 文件过大" in str(exc):
                        raise
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type and content_type not in _allowed_url_content_types(material_type):
                raise ValueError("素材 URL Content-Type 不支持")
            return current
    raise ValueError("素材 URL 重定向次数过多")


def _basic_safe_url(url: str, *, material_type: str) -> str:
    value = url.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.hostname:
        raise ValueError("素材 URL 仅允许 https")
    host = parsed.hostname.strip().lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise ValueError("素材 URL 不允许指向 localhost")
    try:
        _assert_public_ip(ipaddress.ip_address(host.strip("[]")))
    except ValueError as exc:
        if "素材 URL 不允许指向内网或本机地址" in str(exc):
            raise
    path = parsed.path.lower()
    if material_type in URL_MATERIAL_TYPES and path:
        allowed_suffixes = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tgs", ".webm", ".mp4", ".pdf"}
        if "." in Path(path).name and Path(path).suffix not in allowed_suffixes:
            raise ValueError("素材 URL 文件类型不支持")
    return value


def _assert_hostname_resolves_publicly(url: str, *, resolver) -> None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    try:
        addresses = resolver(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("素材 URL DNS 解析失败") from exc
    checked = False
    for item in addresses:
        sockaddr = item[4]
        if not sockaddr:
            continue
        _assert_public_ip(ipaddress.ip_address(str(sockaddr[0])))
        checked = True
    if not checked:
        raise ValueError("素材 URL DNS 解析失败")


def _assert_public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise ValueError("素材 URL 不允许指向内网或本机地址")


def _allowed_url_content_types(material_type: str) -> set[str]:
    if material_type == "图片":
        return IMAGE_MIMES
    if material_type == "表情包":
        return STICKER_MIMES
    return set(get_settings().material_allowed_upload_types)


def validate_material_upload(*, material_type: str, filename: str, content_type: str, data: bytes) -> tuple[str, str]:
    if material_type not in MEDIA_UPLOAD_TYPES:
        raise ValueError("只有图片、表情包和文件素材支持上传")
    if not data:
        raise ValueError("素材文件不能为空")
    settings = get_settings()
    if len(data) > settings.material_max_bytes:
        raise ValueError(f"素材文件过大，最大 {settings.material_max_bytes} 字节")
    normalized_type = (content_type or "").split(";")[0].strip().lower()
    if normalized_type not in settings.material_allowed_upload_types:
        raise ValueError("素材文件类型不支持")
    if material_type == "图片" and normalized_type not in IMAGE_MIMES:
        raise ValueError("图片素材仅支持常见图片格式")
    if material_type == "表情包" and normalized_type not in STICKER_MIMES:
        raise ValueError("表情包素材仅支持图片、TGS 或 video sticker")
    extension = MIME_EXTENSIONS.get(normalized_type) or Path(filename or "").suffix.lower()
    if not extension:
        raise ValueError("无法识别素材文件扩展名")
    return normalized_type, extension


def save_material_upload_temp(*, tenant_id: int, filename: str, content_type: str, data: bytes, material_type: str) -> tuple[Path, str, str]:
    normalized_type, extension = validate_material_upload(material_type=material_type, filename=filename, content_type=content_type, data=data)
    folder = temp_dir("material-tmp") / str(tenant_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid4().hex}{extension}"
    path.write_bytes(data)
    return path, normalized_type, hashlib.sha256(data).hexdigest()


def is_platform_temp_path(path_value: str) -> bool:
    if not path_value:
        return False
    try:
        path = Path(path_value).resolve()
        root = media_root().resolve()
    except OSError:
        return False
    return any(path.is_relative_to((root / subdir).resolve()) for subdir in TEMP_SUBDIRS)


def remove_platform_temp_file(path_value: str) -> bool:
    if not is_platform_temp_path(path_value):
        return False
    path = Path(path_value)
    if path.exists() and path.is_file():
        path.unlink()
        return True
    return False


__all__ = [
    "MEDIA_UPLOAD_TYPES",
    "URL_MATERIAL_TYPES",
    "deep_probe_material_url",
    "is_platform_temp_path",
    "remove_platform_temp_file",
    "save_material_upload_temp",
    "validate_material_upload",
    "validate_material_url",
]
