from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from app.config import Settings, get_settings
from . import telethon_content
from .mock import TelegramGateway
from .contracts import (
    AccountAuthorizationSnapshot,
    AccountHealth,
    AccountSecurityOperationResult,
    ArchiveSnapshot,
    CachedMediaResult,
    ChannelMembershipResult,
    ChannelCommentSnapshot,
    ChannelMessageSnapshot,
    ContactSnapshot,
    DeveloperAppCredentials,
    GroupMessageSnapshot,
    GroupSnapshot,
    InviteLinkResult,
    LoginChallenge,
    OperationResult,
    OutboundSegment,
    ProfileUpdateResult,
    RemoteProfile,
    SendResult,
    VerificationCodeSnapshot,
)
from app.models import FailureType
from app.security import decrypt_session
from app.telethon_lifecycle import TelethonClientLifecycle
from .telethon_media import _parse_custom_emoji_source, _telegram_entity_length, send_media_segment
from .telethon_utils import resolve_telethon_target, telethon_send_target
from .search_join import execute_search_join_with_client
from app.timezone import BEIJING_TZ

_resolve_telethon_target = resolve_telethon_target

VERIFICATION_CONTEXT_DEFAULT_LIMIT = 120
VERIFICATION_CONTEXT_PREVIEW_LIMIT = 500
GROUP_PERMISSION_DETAIL = "群无权限或账号不可发言"
TARGET_PERMISSION_DETAIL = "缓存频道不可访问 / 账号无权限"
VERIFICATION_CONFIRM_BUTTON_MARKERS = ("我已加入", "我已关注", "已关注", "完成验证", "完成关注", "确认")


def _button_labels(message: Any) -> list[str]:
    labels: list[str] = []
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            label = _button_label(button)
            if label:
                labels.append(label)
    return labels


def _button_label(button: Any) -> str:
    text = (getattr(button, "text", "") or "").strip()
    url = _button_url(button)
    if url and url not in text:
        return f"{text} ({url})" if text else url
    return text


def _button_text(button: Any) -> str:
    for candidate in (button, getattr(button, "button", None)):
        if candidate is None:
            continue
        text = (getattr(candidate, "text", "") or "").strip()
        if text:
            return text
    return ""


def _button_url(button: Any) -> str:
    for candidate in (button, getattr(button, "button", None)):
        if candidate is None:
            continue
        url = (getattr(candidate, "url", "") or "").strip()
        if url:
            return url
    return ""


def _search_join_client_metadata(payload: dict[str, Any]) -> dict[str, str]:
    metadata = payload.get("client_metadata") if isinstance(payload, dict) else None
    if not isinstance(metadata, dict):
        raise ValueError("search_join client_metadata missing")
    required = ("device_model", "system_version", "app_version", "client_identity_key")
    if any(not str(metadata.get(key) or "").strip() for key in required):
        raise ValueError("search_join client_metadata incomplete")
    return {key: str(value) for key, value in metadata.items()}


def _verification_button_click_target(message: Any) -> tuple[int, int, str] | None:
    first_text_button: tuple[int, int, str] | None = None
    first_button: tuple[int, int, str] | None = None
    for row_index, row in enumerate(getattr(message, "buttons", None) or []):
        for button_index, button in enumerate(row):
            label = _button_label(button)
            text = _button_text(button)
            if not first_button:
                first_button = (row_index, button_index, label)
            url = _button_url(button)
            if text and not url and not first_text_button:
                first_text_button = (row_index, button_index, text)
            if text and not url and any(marker in text for marker in VERIFICATION_CONFIRM_BUTTON_MARKERS):
                return (row_index, button_index, text)
    return first_text_button or first_button


def _first_message_with_buttons(messages: Any) -> Any | None:
    for message in messages or []:
        if getattr(message, "buttons", None):
            return message
    return None


def _verification_message_text(message: Any) -> str:
    parts: list[str] = []
    text = (getattr(message, "message", "") or "").strip()
    if text:
        parts.append(text)
    if getattr(message, "media", None):
        parts.append("[媒体消息]")
    labels = _button_labels(message)
    if labels:
        parts.append(f"[按钮：{' / '.join(labels)}]")
    return " ".join(parts).strip()


def _permission_detail_with_references(detail: str, fallback: str = GROUP_PERMISSION_DETAIL) -> str:
    if not detail:
        return fallback
    return f"{fallback}：{detail}" if _permission_detail_has_references(detail) else fallback


def _permission_detail_has_references(detail: str) -> bool:
    return bool(
        re.search(r"@[A-Za-z0-9_]{4,}", detail)
        or re.search(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:joinchat/|\+)?[A-Za-z0-9_-]{4,}", detail)
    )


def _permission_detail_from_context_rows(rows: list[dict[str, Any]], fallback: str = TARGET_PERMISSION_DETAIL) -> str:
    prompts = _actionable_permission_prompts(rows)
    if not prompts:
        return fallback
    return f"{GROUP_PERMISSION_DETAIL}：{' | '.join(prompts[:3])}"


def _actionable_permission_prompts(rows: list[dict[str, Any]]) -> list[str]:
    prompts: list[str] = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text or not _permission_prompt_is_actionable(text):
            continue
        compact = re.sub(r"\s+", " ", text)[:VERIFICATION_CONTEXT_PREVIEW_LIMIT]
        if compact not in prompts:
            prompts.append(compact)
    return prompts


def _permission_prompt_is_actionable(text: str) -> bool:
    normalized = text.lower()
    if _permission_detail_with_references(text) != GROUP_PERMISSION_DETAIL:
        return True
    markers = (
        "验证",
        "验证码",
        "captcha",
        "code",
        "关注",
        "订阅",
        "加入频道",
        "点击",
        "按钮",
        "[按钮",
        "+",
        "＋",
        "-",
        "－",
        "加",
        "减",
    )
    return any(marker.lower() in normalized for marker in markers)


async def _verification_context_row(message: Any) -> dict[str, Any] | None:
    text = _verification_message_text(message)
    media_summary = _verification_media_summary(message)
    if not text and not media_summary["has_media"]:
        return None
    if not text:
        text = "[媒体消息]"
    sender = await message.get_sender()
    sender_name = (
        getattr(sender, "first_name", None)
        or getattr(sender, "username", None)
        or getattr(sender, "title", None)
        or "未知来源"
    )
    return {
        "message_id": getattr(message, "id", ""),
        "sender": sender_name,
        "text": text[:VERIFICATION_CONTEXT_PREVIEW_LIMIT],
        "sent_at": getattr(message, "date", None),
        **media_summary,
    }


def _verification_media_summary(message: Any) -> dict[str, Any]:
    media = getattr(message, "media", None)
    if not media:
        return {
            "has_media": False,
            "media_message_id": None,
            "media_mime_type": "",
            "media_fingerprint": "",
        }
    mime_type = _message_media_mime_type(message)
    fingerprint_source = "|".join(
        str(part or "")
        for part in [
            getattr(message, "id", ""),
            type(media).__name__,
            mime_type,
            getattr(getattr(media, "document", None), "id", ""),
            getattr(getattr(media, "photo", None), "id", ""),
        ]
    )
    return {
        "has_media": True,
        "media_message_id": getattr(message, "id", ""),
        "media_mime_type": mime_type,
        "media_fingerprint": hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest() if fingerprint_source.strip("|") else "",
    }


def _message_media_mime_type(message: Any) -> str:
    media = getattr(message, "media", None)
    for candidate in [media, getattr(media, "document", None), getattr(media, "photo", None)]:
        mime_type = getattr(candidate, "mime_type", None)
        if mime_type:
            return str(mime_type)
    return ""
_telethon_send_target = telethon_send_target

GROUP_ADMIN_APPROVE_LABEL = "通过（管理员）"


def _telegram_invite_hash(value: str) -> str:
    text = value.strip()
    for marker in ("t.me/+", "t.me/joinchat/", "telegram.me/+", "telegram.me/joinchat/"):
        if marker in text:
            return text.split(marker, 1)[1].split("?", 1)[0].strip("/")
    if text.startswith("+"):
        return text[1:].split("?", 1)[0].strip("/")
    return ""


def _has_group_admin_rights(permissions: Any) -> bool:
    participant = getattr(permissions, "participant", None)
    names = {type(permissions).__name__.lower(), type(participant).__name__.lower() if participant else ""}
    return bool(
        getattr(permissions, "is_admin", False)
        or getattr(permissions, "is_creator", False)
        or any("admin" in name for name in names)
    )


def _text_sending_banned(rights: Any) -> bool:
    return bool(rights and (getattr(rights, "send_messages", False) or getattr(rights, "send_plain", False)))


def _can_send_text_in_group(target: Any, permissions: Any) -> bool:
    if _has_group_admin_rights(permissions):
        return True
    participant = getattr(permissions, "participant", None)
    participant_rights = getattr(participant, "banned_rights", None)
    if _text_sending_banned(participant_rights):
        return False
    if getattr(permissions, "send_messages", None) is not None:
        return bool(getattr(permissions, "send_messages", False))
    if getattr(permissions, "post_messages", False):
        return True
    return not _text_sending_banned(getattr(target, "default_banned_rights", None))


def _is_account_already_in_group(exc: Exception) -> bool:
    text = f"{exc.__class__.__name__} {exc}".lower()
    return "already" in text and ("participant" in text or "member" in text)


def _invite_account_error_detail(detail: str) -> str:
    normalized = detail.lower()
    if "admin" in normalized or "permission" in normalized or "forbidden" in normalized or "无权限" in detail:
        return "救援账号不是目标群管理员或没有邀请权限"
    if "username" in normalized or "phone" in normalized or "could not find" in normalized or "目标实体无法解析" in detail:
        return "被救援账号无法解析或目标群不可访问"
    return detail


def _linked_chat_from_full_channel(full: Any, linked_id: int) -> Any | None:
    for chat in getattr(full, "chats", []) or []:
        chat_id = getattr(chat, "id", None)
        if chat_id is not None and abs(int(chat_id)) == abs(int(linked_id)):
            return chat
    return None


async def _is_group_verification_message(message: Any, bot_name: str) -> bool:
    if not getattr(message, "buttons", None):
        return False
    text = getattr(message, "message", "") or ""
    if GROUP_ADMIN_APPROVE_LABEL not in _message_button_labels(message):
        return False
    sender = await message.get_sender() if hasattr(message, "get_sender") else None
    sender_name = " ".join(filter(None, [getattr(sender, "first_name", "") or "", getattr(sender, "last_name", "") or ""]))
    sender_name = sender_name or getattr(sender, "username", "") or ""
    return bool(getattr(sender, "bot", False) and bot_name in sender_name and "验证码" in text)


def _message_button_labels(message: Any) -> list[str]:
    labels: list[str] = []
    for row in getattr(message, "buttons", None) or []:
        for button in row:
            labels.append(getattr(button, "text", "") or "")
    return labels


async def _click_admin_approve_button(message: Any) -> bool:
    for row_index, row in enumerate(getattr(message, "buttons", None) or []):
        for col_index, button in enumerate(row):
            if getattr(button, "text", "") == GROUP_ADMIN_APPROVE_LABEL:
                await message.click(row_index, col_index)
                return True
    return False


class TelethonTelegramGateway(TelegramGateway):
    """Telethon-backed production adapter.

    Business services stay synchronous and database-oriented; this adapter owns
    the async Telethon client lifecycle and maps Telegram RPC errors into the
    platform's stable Chinese failure taxonomy.

    Uses a persistent background event loop with a client cache to avoid
    creating a new connection for every operation.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self._lifecycle = TelethonClientLifecycle(self.settings)
        self._pending_clients: dict[int, Any] = {}
        self._pending_qr: dict[int, Any] = {}
        self._pending_credentials: dict[int, DeveloperAppCredentials] = {}

    @classmethod
    def _get_or_create_loop(cls) -> asyncio.AbstractEventLoop:
        return TelethonClientLifecycle.get_or_create_loop()

    def _run(self, coro):
        """Schedule a coroutine on the persistent event loop and block for its result."""
        return self._lifecycle.run(coro)

    def _new_client(
        self,
        credentials: DeveloperAppCredentials,
        raw_session: str | None = None,
        client_metadata: dict[str, str] | None = None,
    ) -> Any:
        """Create a fresh, unconnected Telethon client. Used for login flows where the session is not yet established."""
        return self._lifecycle.new_client(credentials, raw_session, client_metadata)

    async def _get_or_create_client(
        self,
        credentials: DeveloperAppCredentials,
        raw_session: str,
        client_metadata: dict[str, str] | None = None,
    ) -> Any:
        """Return a connected Telethon client from the cache, or create and connect a new one."""
        return await self._lifecycle.get_or_create_client(credentials, raw_session, client_metadata)

    @staticmethod
    def _usable_phone(phone: str | None) -> str:
        if not phone or "*" in phone:
            raise RuntimeError("Telethon code login requires an unmasked phone number on the account")
        return phone

    @staticmethod
    def _usable_credentials(credentials: DeveloperAppCredentials | None) -> DeveloperAppCredentials:
        if credentials is None or credentials.api_id <= 0 or not credentials.api_hash:
            raise RuntimeError("Telethon login requires a developer app credential")
        return credentials

    async def _start_login_async(
        self,
        account_id: int,
        method: str,
        phone: str | None,
        credentials: DeveloperAppCredentials,
    ) -> LoginChallenge:
        client = self._new_client(credentials)
        await client.connect()
        if method == "qr":
            qr_login = await client.qr_login()
            self._pending_clients[account_id] = client
            self._pending_qr[account_id] = qr_login
            self._pending_credentials[account_id] = credentials
            return LoginChallenge(status="等待扫码", qr_payload=qr_login.url)

        await client.send_code_request(self._usable_phone(phone))
        self._pending_clients[account_id] = client
        self._pending_credentials[account_id] = credentials
        return LoginChallenge(
            status="等待验证码",
            code_preview=None,
            code_expires_at=datetime.now(BEIJING_TZ) + timedelta(seconds=self.settings.login_code_ttl_seconds),
        )

    def start_login(
        self,
        method: str,
        account_id: int | None = None,
        phone: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> LoginChallenge:
        if account_id is None:
            raise RuntimeError("Telethon login requires account_id")
        return self._run(self._start_login_async(account_id, method, phone, self._usable_credentials(credentials)))

    async def _finish_login_async(
        self,
        account_id: int,
        code: str | None,
        password_2fa: str | None,
        phone: str | None,
    ) -> tuple[str, str]:
        from telethon.errors import SessionPasswordNeededError

        client = self._pending_clients.get(account_id)
        if client is None:
            raise RuntimeError("login flow not started or has expired in this process")
        try:
            if account_id in self._pending_qr:
                qr_login = self._pending_qr[account_id]
                try:
                    # Wait up to 5s per poll; frontend should poll every ~3-5s
                    await asyncio.wait_for(qr_login.wait(), timeout=5)
                except TimeoutError:
                    return "等待扫码", ""
            elif password_2fa:
                await client.sign_in(password=password_2fa)
            else:
                await client.sign_in(phone=self._usable_phone(phone), code=code)
        except SessionPasswordNeededError:
            return "等待2FA", ""

        raw_session = client.session.save()
        # Cache the now-logged-in client under its final session string
        credentials = self._pending_credentials.pop(account_id, None)
        if credentials is not None:
            await self._lifecycle.remember_connected_client(credentials, raw_session, client)
        else:
            await client.disconnect()
        self._pending_clients.pop(account_id, None)
        self._pending_qr.pop(account_id, None)
        return "在线", raw_session

    def finish_login(
        self,
        code: str | None,
        password_2fa: str | None,
        account_id: int | None = None,
        phone: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> tuple[str, str]:
        if account_id is None:
            raise RuntimeError("Telethon login verification requires account_id")
        return self._run(self._finish_login_async(account_id, code, password_2fa, phone))

    async def _health_async(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
    ) -> AccountHealth:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return AccountHealth(status="需重新登录", health_score=45, detail="账号没有可用 session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return AccountHealth(status="需重新登录", health_score=45, detail="session 已失效")
        await client.get_me()
        return AccountHealth(status="在线", health_score=95, detail="账号 session 可用")

    def check_account_health(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> AccountHealth:
        return self._run(self._health_async(session_ciphertext, self._usable_credentials(credentials)))

    async def _list_authorizations_async(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
    ) -> list[AccountAuthorizationSnapshot]:
        client = await self._authorized_client(session_ciphertext, credentials, error_message="账号没有可用 session")
        from telethon import functions

        response = await client(functions.account.GetAuthorizationsRequest())
        snapshots: list[AccountAuthorizationSnapshot] = []
        for authorization in getattr(response, "authorizations", []) or []:
            snapshots.append(
                AccountAuthorizationSnapshot(
                    authorization_hash=str(getattr(authorization, "hash", "")),
                    is_current=bool(getattr(authorization, "current", False)),
                    device_model=getattr(authorization, "device_model", "") or "",
                    platform=getattr(authorization, "platform", "") or "",
                    system_version=getattr(authorization, "system_version", "") or "",
                    api_id=int(getattr(authorization, "api_id", 0) or 0),
                    app_name=getattr(authorization, "app_name", "") or "",
                    app_version=getattr(authorization, "app_version", "") or "",
                    ip=getattr(authorization, "ip", "") or "",
                    country=getattr(authorization, "country", "") or "",
                    region=getattr(authorization, "region", "") or "",
                    date_created=getattr(authorization, "date_created", None),
                    date_active=getattr(authorization, "date_active", None),
                )
            )
        return snapshots

    def list_authorizations(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> list[AccountAuthorizationSnapshot]:
        return self._run(self._list_authorizations_async(session_ciphertext, self._usable_credentials(credentials)))

    async def _cleanup_authorization_async(
        self,
        session_ciphertext: str | None,
        authorization_hash: str,
        credentials: DeveloperAppCredentials,
    ) -> AccountSecurityOperationResult:
        if not authorization_hash:
            return AccountSecurityOperationResult(False, "失败", "授权Hash缺失", "登录设备授权标识为空")
        client = await self._authorized_client(session_ciphertext, credentials, error_message="账号没有可用 session")
        from telethon import functions

        try:
            await client(functions.account.ResetAuthorizationRequest(hash=int(authorization_hash)))
        except Exception as exc:  # noqa: BLE001 - mapped for operator-facing batch detail.
            mapped = self._map_send_error(exc)
            return AccountSecurityOperationResult(False, "失败", mapped.failure_type or FailureType.UNKNOWN.value, mapped.detail or str(exc))
        return AccountSecurityOperationResult(True, "已清理", detail="外部设备已退出")

    def cleanup_authorization(
        self,
        session_ciphertext: str | None,
        authorization_hash: str,
        credentials: DeveloperAppCredentials | None = None,
    ) -> AccountSecurityOperationResult:
        return self._run(self._cleanup_authorization_async(session_ciphertext, authorization_hash, self._usable_credentials(credentials)))

    async def _get_two_fa_status_async(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
    ) -> AccountSecurityOperationResult:
        client = await self._authorized_client(session_ciphertext, credentials, error_message="账号没有可用 session")
        from telethon import functions

        try:
            password = await client(functions.account.GetPasswordRequest())
        except Exception as exc:  # noqa: BLE001 - operator-facing security detail.
            mapped = self._map_send_error(exc)
            return AccountSecurityOperationResult(False, "unknown", mapped.failure_type or FailureType.UNKNOWN.value, mapped.detail or str(exc))
        status = "enabled" if getattr(password, "has_password", False) or getattr(password, "current_algo", None) else "missing"
        if getattr(password, "email_unconfirmed_pattern", None):
            status = "email_confirmation_required"
        return AccountSecurityOperationResult(True, status, detail=status)

    def get_two_fa_status(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> AccountSecurityOperationResult:
        return self._run(self._get_two_fa_status_async(session_ciphertext, self._usable_credentials(credentials)))

    async def _set_two_fa_password_async(
        self,
        session_ciphertext: str | None,
        password: str,
        credentials: DeveloperAppCredentials,
        *,
        hint: str = "platform managed",
        current_password: str | None = None,
    ) -> AccountSecurityOperationResult:
        client = await self._authorized_client(session_ciphertext, credentials, error_message="账号没有可用 session")
        try:
            changed = await client.edit_2fa(current_password=current_password, new_password=password, hint=hint)
        except Exception as exc:  # noqa: BLE001 - keep Telegram restriction visible.
            mapped = self._map_send_error(exc)
            detail = mapped.detail or str(exc)
            status = "email_confirmation_required" if "email" in detail.lower() else "failed"
            return AccountSecurityOperationResult(False, status, mapped.failure_type or FailureType.UNKNOWN.value, detail)
        return AccountSecurityOperationResult(True, "enabled" if changed else "unchanged", detail="二步验证已设置")

    def set_two_fa_password(
        self,
        session_ciphertext: str | None,
        password: str,
        credentials: DeveloperAppCredentials | None = None,
        *,
        hint: str = "platform managed",
        current_password: str | None = None,
    ) -> AccountSecurityOperationResult:
        return self._run(
            self._set_two_fa_password_async(
                session_ciphertext,
                password,
                self._usable_credentials(credentials),
                hint=hint,
                current_password=current_password,
            )
        )

    async def _confirm_two_fa_email_async(
        self,
        session_ciphertext: str | None,
        code: str,
        credentials: DeveloperAppCredentials,
    ) -> AccountSecurityOperationResult:
        if not code:
            return AccountSecurityOperationResult(False, "失败", "邮箱验证码缺失", "请输入 Telegram 恢复邮箱验证码")
        client = await self._authorized_client(session_ciphertext, credentials, error_message="账号没有可用 session")
        from telethon import functions

        try:
            await client(functions.account.ConfirmPasswordEmailRequest(code=code))
        except Exception as exc:  # noqa: BLE001 - operator-facing 2FA email detail.
            mapped = self._map_send_error(exc)
            return AccountSecurityOperationResult(False, "failed", mapped.failure_type or FailureType.UNKNOWN.value, mapped.detail or str(exc))
        return AccountSecurityOperationResult(True, "enabled", detail="二步验证恢复邮箱已确认")

    def confirm_two_fa_email(
        self,
        session_ciphertext: str | None,
        code: str,
        credentials: DeveloperAppCredentials | None = None,
    ) -> AccountSecurityOperationResult:
        return self._run(self._confirm_two_fa_email_async(session_ciphertext, code, self._usable_credentials(credentials)))

    async def _update_username_async(
        self,
        session_ciphertext: str | None,
        username: str,
        credentials: DeveloperAppCredentials,
    ) -> AccountSecurityOperationResult:
        client = await self._authorized_client(session_ciphertext, credentials, error_message="账号没有可用 session")
        from telethon import functions

        try:
            await client(functions.account.UpdateUsernameRequest(username=username))
        except Exception as exc:  # noqa: BLE001 - username conflicts/flood are normal batch outcomes.
            mapped = self._map_send_error(exc)
            return AccountSecurityOperationResult(False, "失败", mapped.failure_type or FailureType.UNKNOWN.value, mapped.detail or str(exc))
        return AccountSecurityOperationResult(True, "已完成", detail=username)

    def update_username(
        self,
        session_ciphertext: str | None,
        username: str,
        credentials: DeveloperAppCredentials | None = None,
    ) -> AccountSecurityOperationResult:
        return self._run(self._update_username_async(session_ciphertext, username, self._usable_credentials(credentials)))

    async def _authorized_client(self, session_ciphertext: str | None, credentials: DeveloperAppCredentials, *, error_message: str) -> Any:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            raise RuntimeError(error_message)
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            raise RuntimeError("session is not authorized")
        return client

    async def _groups_async(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
    ) -> list[GroupSnapshot]:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            raise RuntimeError("sync groups requires a valid session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            raise RuntimeError("session is not authorized")
        from telethon import utils
        snapshots: list[GroupSnapshot] = []
        seen_peer_ids: set[str] = set()
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not (dialog.is_group or dialog.is_channel):
                continue
            resolved_entity = getattr(entity, "migrated_to", None) or entity
            peer_id = str(utils.get_peer_id(resolved_entity))
            if peer_id in seen_peer_ids:
                continue
            seen_peer_ids.add(peer_id)
            default_banned = getattr(entity, "default_banned_rights", None)
            can_send = not bool(default_banned and getattr(default_banned, "send_messages", False))
            snapshots.append(
                GroupSnapshot(
                    tg_peer_id=peer_id,
                    title=dialog.name or "未命名群聊",
                    group_type=(
                        "supergroup"
                        if getattr(entity, "migrated_to", None)
                        else "channel"
                        if dialog.is_channel and not dialog.is_group
                        else "supergroup"
                        if dialog.is_channel
                        else "group"
                    ),
                    member_count=int(getattr(entity, "participants_count", 0) or 0),
                    permission_label="可发言" if can_send else "不可发言",
                    can_send=can_send,
                    slowmode_seconds=getattr(entity, "slowmode_seconds", None),
                    username=getattr(entity, "username", None),
                )
            )
        return snapshots

    def list_groups(
        self,
        account_id: int,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> list[GroupSnapshot]:
        return self._run(self._groups_async(session_ciphertext, self._usable_credentials(credentials)))

    async def _verification_codes_async(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
    ) -> list[VerificationCodeSnapshot]:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            raise RuntimeError("poll verification codes requires a valid session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            raise RuntimeError("session is not authorized")
        messages = await client.get_messages(777000, limit=10)
        import re

        snapshots: list[VerificationCodeSnapshot] = []
        for message in messages:
            text = getattr(message, "message", "") or ""
            match = re.search(r"(?<!\d)(\d{5,6})(?!\d)", text)
            if match:
                snapshots.append(
                    VerificationCodeSnapshot(
                        code=match.group(1),
                        raw_hint="TG 官方服务消息验证码",
                        expires_at=datetime.now(BEIJING_TZ) + timedelta(seconds=self.settings.login_code_ttl_seconds),
                    )
                )
                break
        return snapshots

    def poll_verification_codes(
        self,
        account_id: int,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> list[VerificationCodeSnapshot]:
        return self._run(self._verification_codes_async(session_ciphertext, self._usable_credentials(credentials)))

    async def _contacts_async(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
    ) -> list[ContactSnapshot]:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            raise RuntimeError("sync contacts requires a valid session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            raise RuntimeError("session is not authorized")
        seen_peer_ids: set[str] = set()
        snapshots: list[ContactSnapshot] = []
        async for dialog in client.iter_dialogs():
            if not dialog.is_user:
                continue
            entity = dialog.entity
            user_id = str(getattr(entity, "id", dialog.id))
            if user_id in seen_peer_ids:
                continue
            seen_peer_ids.add(user_id)
            first_name = getattr(entity, "first_name", "") or ""
            last_name = getattr(entity, "last_name", "") or ""
            display_name = (dialog.name or f"{first_name} {last_name}".strip() or user_id).strip()
            snapshots.append(
                ContactSnapshot(
                    peer_id=user_id,
                    display_name=display_name,
                    username=getattr(entity, "username", None),
                    phone=getattr(entity, "phone", None),
                    contact_type="private",
                    is_mutual=bool(getattr(entity, "mutual_contact", False)),
                    last_message_at=getattr(dialog, "date", None),
                )
            )
        if len(snapshots) < 120:
            async for dialog in client.iter_dialogs():
                if not (dialog.is_group or dialog.is_channel):
                    continue
                try:
                    async for participant in client.iter_participants(dialog.entity, limit=30):
                        user_id = str(getattr(participant, "id", ""))
                        if not user_id or user_id in seen_peer_ids:
                            continue
                        seen_peer_ids.add(user_id)
                        first_name = getattr(participant, "first_name", "") or ""
                        last_name = getattr(participant, "last_name", "") or ""
                        display_name = f"{first_name} {last_name}".strip() or getattr(participant, "username", None) or user_id
                        snapshots.append(
                            ContactSnapshot(
                                peer_id=user_id,
                                display_name=f"{display_name}（{dialog.name}）",
                                username=getattr(participant, "username", None),
                                phone=getattr(participant, "phone", None),
                                contact_type="group_member",
                                is_mutual=bool(getattr(participant, "mutual_contact", False)),
                                last_message_at=None,
                            )
                        )
                        if len(snapshots) >= 120:
                            break
                except Exception:
                    continue
                if len(snapshots) >= 120:
                    break
        return snapshots

    def list_contacts(
        self,
        account_id: int,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> list[ContactSnapshot]:
        return self._run(self._contacts_async(session_ciphertext, self._usable_credentials(credentials)))

    @staticmethod
    def _map_send_error(exc: Exception) -> SendResult:
        from telethon import errors

        detail = str(exc) or exc.__class__.__name__
        invalid_entity_markers = (
            "Could not find the input entity",
            "Cannot cast InputPeerUser to any kind of InputChannel",
        )
        if any(marker in detail for marker in invalid_entity_markers):
            return SendResult(False, failure_type=FailureType.PEER_INVALID.value, detail="目标实体无法解析，请重新同步账号群聊/运营目标后再试")
        comment_thread_markers = (
            "GetDiscussionMessageRequest",
            "DiscussionMessage",
            "message ID used in the peer was invalid",
        )
        if any(marker.lower() in detail.lower() for marker in comment_thread_markers):
            return SendResult(
                False,
                failure_type=FailureType.COMMENT_UNAVAILABLE.value,
                detail="频道帖子无法解析到评论区，请确认消息ID属于频道帖子、频道已绑定讨论组，且执行账号可进入讨论组并评论",
            )
        reaction_unavailable_markers = (
            "SendReactionRequest",
            "can't do that operation on such message",
            "specified message ID is invalid",
        )
        if any(marker.lower() in detail.lower() for marker in reaction_unavailable_markers):
            return SendResult(False, failure_type=FailureType.REACTION_UNAVAILABLE.value, detail="频道消息不可点赞或消息ID无效")
        membership_required_markers = (
            "UserNotParticipant",
            "not a participant",
            "not participant",
            "not a member",
        )
        if any(marker.lower() in detail.lower() for marker in membership_required_markers):
            return SendResult(False, failure_type=FailureType.GROUP_PERMISSION_DENIED.value, detail="账号未关注/未加入目标频道或无法进入关联讨论区")
        custom_emoji_markers = ("CUSTOM_EMOJI", "MessageEntityCustomEmoji", "custom emoji", "document id")
        if any(marker.lower() in detail.lower() for marker in custom_emoji_markers):
            return SendResult(False, failure_type="custom_emoji_unavailable", detail="custom emoji 当前账号或目标不可用")
        send_permission_markers = (
            "channel specified is private and you lack permission",
            "another reason may be that you were banned from it",
        )
        lower_detail = detail.lower()
        permission_request_markers = ("sendmessagerequest", "joinchannelrequest", "importchatinviterequest")
        if any(request in lower_detail for request in permission_request_markers) and any(marker in lower_detail for marker in send_permission_markers):
            return SendResult(False, failure_type=FailureType.GROUP_PERMISSION_DENIED.value, detail=_permission_detail_with_references(detail))
        if "joinchannelrequest" in lower_detail and "successfully requested to join" in lower_detail:
            return SendResult(False, failure_type=FailureType.GROUP_PERMISSION_DENIED.value, detail="已提交入群申请，等待审批后才能发言")
        if isinstance(exc, errors.FloodWaitError):
            return SendResult(False, failure_type=FailureType.FLOOD_WAIT.value, detail=f"FloodWait {exc.seconds} 秒")
        if isinstance(exc, getattr(errors, "SlowModeWaitError", ())):
            seconds = getattr(exc, "seconds", None)
            return SendResult(False, failure_type=FailureType.SLOWMODE.value, detail=f"群慢速模式，需等待 {seconds or '一段时间'} 秒")
        if isinstance(exc, (errors.ChatWriteForbiddenError, errors.ChatAdminRequiredError)):
            return SendResult(False, failure_type=FailureType.GROUP_PERMISSION_DENIED.value, detail="群无权限或账号不可发言")
        account_limited_errors = tuple(
            item
            for item in (
                getattr(errors, "UserBannedInChannelError", None),
                getattr(errors, "UserDeactivatedError", None),
                getattr(errors, "UserDeactivatedBanError", None),
            )
            if item is not None
        )
        invalid_target_errors = tuple(
            item
            for item in (
                getattr(errors, "PeerIdInvalidError", None),
                getattr(errors, "UsernameInvalidError", None),
                getattr(errors, "ChannelInvalidError", None),
            )
            if item is not None
        )
        if account_limited_errors and isinstance(exc, account_limited_errors):
            return SendResult(False, failure_type=FailureType.ACCOUNT_LIMITED.value, detail="账号受限或不可用")
        if invalid_target_errors and isinstance(exc, invalid_target_errors):
            return SendResult(False, failure_type=FailureType.PEER_INVALID.value, detail="目标群无效或不可访问")
        return SendResult(False, failure_type=FailureType.UNKNOWN.value, detail=detail)

    _parse_custom_emoji_source = staticmethod(_parse_custom_emoji_source)
    _telegram_entity_length = staticmethod(_telegram_entity_length)

    async def _send_async(
        self,
        session_ciphertext: str | None,
        peer_id: str | None,
        content: str,
        segments: list[OutboundSegment] | None,
        credentials: DeveloperAppCredentials,
        *,
        group_id: int = 0,
        reply_to_message_id: int | None = None,
    ) -> SendResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return SendResult(False, failure_type=FailureType.ACCOUNT_UNAVAILABLE.value, detail="账号没有可用 session")
        if not peer_id:
            return SendResult(False, failure_type=FailureType.PEER_INVALID.value, detail="缺少 TG peer id")

        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return SendResult(False, failure_type=FailureType.ACCOUNT_UNAVAILABLE.value, detail="session 已失效")
        try:
            target = await resolve_telethon_target(client, peer_id, group_id=group_id)
            remote_message_id: str | None = None
            if segments:
                for segment in segments:
                    if segment.segment_type == "文本":
                        message = await client.send_message(target, segment.content, reply_to=reply_to_message_id)
                    elif segment.segment_type == "链接":
                        text = "\n".join(piece for piece in [segment.content, segment.source] if piece).strip()
                        message = await client.send_message(target, text, reply_to=reply_to_message_id)
                    else:
                        message = await send_media_segment(client, target, segment, reply_to_message_id=reply_to_message_id)
                    remote_message_id = str(getattr(message, "id", remote_message_id or uuid4().hex[:8]))
            else:
                message = await client.send_message(target, content, reply_to=reply_to_message_id)
                remote_message_id = str(message.id)
            return SendResult(True, remote_message_id=remote_message_id)
        except Exception as exc:  # Telethon exposes many RPC subclasses; map them at the adapter boundary.
            return self._map_send_error(exc)

    def send_message(
        self,
        account_id: int,
        group_id: int,
        content: str,
        segments: list[OutboundSegment] | None = None,
        session_ciphertext: str | None = None,
        peer_id: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
        reply_to_message_id: int | None = None,
    ) -> SendResult:
        return self._run(
            self._send_async(
                session_ciphertext,
                peer_id,
                content,
                segments,
                self._usable_credentials(credentials),
                group_id=group_id,
                reply_to_message_id=reply_to_message_id,
            )
        )

    async def _execute_search_join_async(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
        payload: dict[str, Any],
        keyword_text: str,
    ) -> dict[str, Any]:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return {"success": False, "error_code": FailureType.ACCOUNT_UNAVAILABLE.value, "detail": "账号没有可用 session"}
        client = await self._get_or_create_client(credentials, raw_session, _search_join_client_metadata(payload))
        if not await client.is_user_authorized():
            return {"success": False, "error_code": FailureType.ACCOUNT_UNAVAILABLE.value, "detail": "session 已失效"}
        return await execute_search_join_with_client(client, payload, keyword_text=keyword_text)

    def execute_search_join(
        self,
        account_id: int,
        payload: dict[str, Any],
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
        keyword_text: str = "",
    ) -> dict[str, Any]:
        return self._run(
            self._execute_search_join_async(session_ciphertext, self._usable_credentials(credentials), payload, keyword_text)
        )

    async def _view_channel_message_async(
        self,
        session_ciphertext: str | None,
        channel_peer_id: str,
        message_id: int,
        credentials: DeveloperAppCredentials,
    ) -> OperationResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "session 已失效")
        try:
            from telethon import functions

            target: int | str = int(channel_peer_id) if channel_peer_id.lstrip("-").isdigit() else channel_peer_id
            entity = await client.get_entity(target)
            await client(functions.messages.GetMessagesViewsRequest(peer=entity, id=[message_id], increment=True))
            return OperationResult(True, detail=f"message_id={message_id}")
        except Exception as exc:
            mapped = self._map_send_error(exc)
            return OperationResult(False, "失败", mapped.failure_type or FailureType.UNKNOWN.value, mapped.detail or str(exc))

    def view_channel_message(
        self,
        account_id: int,
        channel_peer_id: str,
        message_id: int,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return self._run(self._view_channel_message_async(session_ciphertext, channel_peer_id, message_id, self._usable_credentials(credentials)))

    async def _ensure_channel_membership_async(
        self,
        session_ciphertext: str | None,
        channel_peer_id: str,
        credentials: DeveloperAppCredentials,
        invite_link: str = "",
    ) -> ChannelMembershipResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return ChannelMembershipResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session", "failed")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return ChannelMembershipResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "session 已失效", "failed")
        try:
            from telethon import functions
            from telethon.errors import UserAlreadyParticipantError

            target = (invite_link or channel_peer_id or "").strip()
            if not target:
                return ChannelMembershipResult(False, "失败", FailureType.PEER_INVALID.value, "缺少频道地址", "failed")
            invite_hash = _telegram_invite_hash(target)
            if invite_hash:
                try:
                    await client(functions.messages.ImportChatInviteRequest(invite_hash))
                except UserAlreadyParticipantError:
                    return ChannelMembershipResult(True, detail="already_joined", membership_status="already_joined")
            else:
                entity_ref: int | str = int(target) if target.lstrip("-").isdigit() else target.lstrip("@")
                entity = await client.get_entity(entity_ref)
                await client(functions.channels.JoinChannelRequest(entity))
            return ChannelMembershipResult(True, detail="joined", membership_status="joined")
        except Exception as exc:
            mapped = self._map_send_error(exc)
            return ChannelMembershipResult(False, "失败", mapped.failure_type or FailureType.PEER_INVALID.value, mapped.detail or str(exc), "failed")

    def ensure_channel_membership(
        self,
        account_id: int,
        channel_peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
        *,
        invite_link: str = "",
    ) -> ChannelMembershipResult:
        return self._run(self._ensure_channel_membership_async(session_ciphertext, channel_peer_id, self._usable_credentials(credentials), invite_link))

    async def _export_group_invite_link_async(
        self,
        account_id: int,
        session_ciphertext: str | None,
        group_peer_id: str,
        credentials: DeveloperAppCredentials,
    ) -> InviteLinkResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return InviteLinkResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return InviteLinkResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "session 已失效")
        try:
            from telethon import functions

            target = (group_peer_id or "").strip()
            if not target:
                return InviteLinkResult(False, "失败", FailureType.PEER_INVALID.value, "缺少目标群")
            entity_ref: int | str = int(target) if target.lstrip("-").isdigit() else target.lstrip("@")
            entity = await client.get_entity(entity_ref)
            invite = await client(
                functions.messages.ExportChatInviteRequest(
                    peer=entity,
                    title=f"tg-yunying-rescue-{account_id}",
                )
            )
            link = str(getattr(invite, "link", "") or "").strip()
            if not link:
                return InviteLinkResult(False, "失败", "invite_link_empty", "Telegram 未返回邀请链接")
            return InviteLinkResult(True, detail=link, invite_link=link)
        except Exception as exc:
            mapped = self._map_send_error(exc)
            return InviteLinkResult(False, "失败", mapped.failure_type or "invite_export_failed", mapped.detail or str(exc))

    def export_group_invite_link(
        self,
        account_id: int,
        group_peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> InviteLinkResult:
        return self._run(self._export_group_invite_link_async(account_id, session_ciphertext, group_peer_id, self._usable_credentials(credentials)))

    async def _lift_group_account_restrictions_async(
        self,
        session_ciphertext: str | None,
        group_peer_id: str,
        target_account_ref: str,
        credentials: DeveloperAppCredentials,
    ) -> OperationResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "session 已失效")
        try:
            from telethon import functions, types

            target = await resolve_telethon_target(client, group_peer_id, group_id=0)
            user = await client.get_entity(target_account_ref.strip().lstrip("@"))
            rights = types.ChatBannedRights(
                until_date=None,
                view_messages=False,
                send_messages=False,
                send_media=False,
                send_plain=False,
            )
            await client(functions.channels.EditBannedRequest(channel=target, participant=user, banned_rights=rights))
            return OperationResult(True, "已处理", detail="account_restrictions_lifted")
        except Exception as exc:
            mapped = self._map_send_error(exc)
            detail = _invite_account_error_detail(mapped.detail or str(exc))
            return OperationResult(False, "失败", mapped.failure_type or FailureType.UNKNOWN.value, detail)

    def lift_group_account_restrictions(
        self,
        account_id: int,
        group_peer_id: str,
        target_account_ref: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return self._run(
            self._lift_group_account_restrictions_async(
                session_ciphertext,
                group_peer_id,
                target_account_ref,
                self._usable_credentials(credentials),
            )
        )

    async def _approve_group_join_request_async(
        self,
        session_ciphertext: str | None,
        group_peer_id: str,
        target_account_ref: str,
        credentials: DeveloperAppCredentials,
    ) -> OperationResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "session 已失效")
        try:
            from telethon import functions

            target = await resolve_telethon_target(client, group_peer_id, group_id=0)
            user = await client.get_entity(target_account_ref.strip().lstrip("@"))
            await client(functions.messages.HideChatJoinRequestRequest(peer=target, user_id=user, approved=True))
            return OperationResult(True, "已处理", detail="join_request_approved")
        except Exception as exc:
            mapped = self._map_send_error(exc)
            detail = _invite_account_error_detail(mapped.detail or str(exc))
            return OperationResult(False, "失败", mapped.failure_type or FailureType.UNKNOWN.value, detail)

    def approve_group_join_request(
        self,
        account_id: int,
        group_peer_id: str,
        target_account_ref: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return self._run(
            self._approve_group_join_request_async(
                session_ciphertext,
                group_peer_id,
                target_account_ref,
                self._usable_credentials(credentials),
            )
        )

    async def _invite_account_to_group_async(
        self,
        session_ciphertext: str | None,
        group_peer_id: str,
        target_account_ref: str,
        credentials: DeveloperAppCredentials,
    ) -> OperationResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "session 已失效")
        try:
            from telethon import functions

            target = await resolve_telethon_target(client, group_peer_id, group_id=0)
            user = await client.get_entity(target_account_ref.strip().lstrip("@"))
            await client(functions.channels.InviteToChannelRequest(channel=target, users=[user]))
            return OperationResult(True, "已处理", detail="account_invited")
        except Exception as exc:
            if _is_account_already_in_group(exc):
                return OperationResult(True, "已处理", detail="account_already_present")
            mapped = self._map_send_error(exc)
            detail = _invite_account_error_detail(mapped.detail or str(exc))
            return OperationResult(False, "失败", mapped.failure_type or FailureType.UNKNOWN.value, detail)

    def invite_account_to_group(
        self,
        account_id: int,
        group_peer_id: str,
        target_account_ref: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return self._run(
            self._invite_account_to_group_async(
                session_ciphertext,
                group_peer_id,
                target_account_ref,
                self._usable_credentials(credentials),
            )
        )

    async def _ensure_linked_channel_membership_async(
        self,
        session_ciphertext: str | None,
        group_peer_id: str,
        credentials: DeveloperAppCredentials,
    ) -> OperationResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "session 已失效")
        try:
            from telethon import functions, types
            from telethon.errors import UserAlreadyParticipantError

            group = await resolve_telethon_target(client, group_peer_id, group_id=0)
            full = await client(functions.channels.GetFullChannelRequest(group))
            linked_id = getattr(getattr(full, "full_chat", None), "linked_chat_id", None)
            if not linked_id:
                return OperationResult(False, "失败", FailureType.GROUP_PERMISSION_DENIED.value, "未解析到群关联频道")
            linked = _linked_chat_from_full_channel(full, linked_id)
            if linked is None:
                linked = await client.get_entity(types.PeerChannel(int(linked_id)))
            try:
                await client(functions.channels.JoinChannelRequest(linked))
            except UserAlreadyParticipantError:
                pass
            title = getattr(linked, "title", "") or str(linked_id)
            return OperationResult(True, "已处理", detail=f"已关注关联频道：{title}")
        except Exception as exc:
            mapped = self._map_send_error(exc)
            return OperationResult(False, "失败", mapped.failure_type or FailureType.UNKNOWN.value, mapped.detail or str(exc))

    def ensure_linked_channel_membership(
        self,
        account_id: int,
        group_peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return self._run(self._ensure_linked_channel_membership_async(session_ciphertext, group_peer_id, self._usable_credentials(credentials)))

    async def _send_channel_reaction_async(
        self,
        session_ciphertext: str | None,
        channel_peer_id: str,
        message_id: int,
        reaction: str,
        credentials: DeveloperAppCredentials,
    ) -> OperationResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "session 已失效")
        try:
            from telethon import functions, types

            target: int | str = int(channel_peer_id) if channel_peer_id.lstrip("-").isdigit() else channel_peer_id
            entity = await client.get_entity(target)
            await client(
                functions.messages.SendReactionRequest(
                    peer=entity,
                    msg_id=message_id,
                    reaction=[types.ReactionEmoji(emoticon=reaction or "👍")],
                )
            )
            return OperationResult(True, detail=f"reaction={reaction or '👍'}; message_id={message_id}")
        except Exception as exc:
            mapped = self._map_send_error(exc)
            failure = mapped.failure_type or FailureType.REACTION_UNAVAILABLE.value
            return OperationResult(False, "失败", failure, mapped.detail or str(exc) or "Reaction不可用")

    def send_channel_reaction(
        self,
        account_id: int,
        channel_peer_id: str,
        message_id: int,
        reaction: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return self._run(self._send_channel_reaction_async(session_ciphertext, channel_peer_id, message_id, reaction, self._usable_credentials(credentials)))

    async def _reply_channel_message_async(
        self,
        session_ciphertext: str | None,
        channel_peer_id: str,
        message_id: int,
        content: str,
        credentials: DeveloperAppCredentials,
        reply_to_message_id: int | None = None,
    ) -> SendResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return SendResult(False, failure_type=FailureType.ACCOUNT_UNAVAILABLE.value, detail="账号没有可用 session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return SendResult(False, failure_type=FailureType.ACCOUNT_UNAVAILABLE.value, detail="session 已失效")
        try:
            target: int | str = int(channel_peer_id) if channel_peer_id.lstrip("-").isdigit() else channel_peer_id
            entity = await client.get_entity(target)
            send_kwargs = {"comment_to": message_id}
            if reply_to_message_id:
                send_kwargs["reply_to"] = reply_to_message_id
            message = await client.send_message(entity, content, **send_kwargs)
            return SendResult(True, remote_message_id=str(getattr(message, "id", "")))
        except Exception as exc:
            mapped = self._map_send_error(exc)
            return SendResult(False, failure_type=mapped.failure_type or FailureType.COMMENT_UNAVAILABLE.value, detail=mapped.detail or "频道消息不支持回复")

    def reply_channel_message(
        self,
        account_id: int,
        channel_peer_id: str,
        message_id: int,
        content: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
        *,
        reply_to_message_id: int | None = None,
    ) -> SendResult:
        return self._run(self._reply_channel_message_async(session_ciphertext, channel_peer_id, message_id, content, self._usable_credentials(credentials), reply_to_message_id))

    def send_message_to_target(
        self,
        account_id: int,
        target_peer_id: str,
        content: str,
        target_type: str = "group",
        segments: list[OutboundSegment] | None = None,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> SendResult:
        return self.send_message(account_id, 0, content, segments, session_ciphertext, target_peer_id, credentials)

    async def _delete_message_async(
        self,
        session_ciphertext: str | None,
        target_peer_id: str,
        message_id: str,
        credentials: DeveloperAppCredentials,
    ) -> OperationResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "session 已失效")
        try:
            target = await resolve_telethon_target(client, target_peer_id, group_id=0)
            await client.delete_messages(target, [int(message_id)])
            return OperationResult(True, detail=f"message_id={message_id}")
        except ValueError:
            return OperationResult(False, "失败", FailureType.PEER_INVALID.value, "删除消息 ID 非数字")
        except Exception as exc:
            mapped = self._map_send_error(exc)
            return OperationResult(False, "失败", mapped.failure_type or FailureType.UNKNOWN.value, mapped.detail or str(exc))

    def delete_message(
        self,
        account_id: int,
        target_peer_id: str,
        message_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return self._run(self._delete_message_async(session_ciphertext, target_peer_id, message_id, self._usable_credentials(credentials)))

    def probe_target_capabilities(
        self,
        account_id: int,
        target_peer_id: str,
        target_type: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        if not credentials:
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "开发者应用不可用")
        return self._run(
            self._probe_target_capabilities_async(
                raw_session,
                target_peer_id,
                target_type,
                self._usable_credentials(credentials),
            )
        )

    async def _probe_target_capabilities_async(
        self,
        raw_session: str,
        target_peer_id: str,
        target_type: str,
        credentials: DeveloperAppCredentials,
    ) -> OperationResult:
        client = await self._get_or_create_client(credentials, raw_session)
        result = await self._probe_target_capabilities_with_client(client, target_peer_id, target_type)
        if not result.ok:
            await self._lifecycle.invalidate_client(credentials, raw_session)
        return result

    async def _probe_target_capabilities_with_client(
        self,
        client: Any,
        target_peer_id: str,
        target_type: str,
    ) -> OperationResult:
        if not await client.is_user_authorized():
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "session 已失效")
        target = None
        try:
            target = await resolve_telethon_target(client, target_peer_id, group_id=0)
            default_rights = getattr(target, "default_banned_rights", None)
            if default_rights and getattr(default_rights, "send_messages", False):
                detail = await self._permission_detail_from_target_context(client, target)
                return OperationResult(False, "失败", FailureType.GROUP_PERMISSION_DENIED.value, detail)
            if hasattr(client, "get_permissions"):
                try:
                    permissions = await client.get_permissions(target, "me")
                    if not _can_send_text_in_group(target, permissions):
                        detail = await self._permission_detail_from_target_context(client, target)
                        return OperationResult(False, "失败", FailureType.GROUP_PERMISSION_DENIED.value, detail)
                except Exception as exc:
                    mapped = self._map_send_error(exc)
                    detail = await self._permission_detail_from_probe_exception(client, target, mapped)
                    return OperationResult(False, "失败", FailureType.GROUP_PERMISSION_DENIED.value, detail)
            else:
                return OperationResult(False, "失败", FailureType.GROUP_PERMISSION_DENIED.value, TARGET_PERMISSION_DETAIL)
            return OperationResult(True, detail=f"{target_type}:{target_peer_id}:可访问")
        except Exception as exc:  # Telethon exposes many RPC subclasses; map them at the adapter boundary.
            mapped = self._map_send_error(exc)
            detail = (
                await self._permission_detail_from_probe_exception(client, target, mapped)
                if target is not None
                else mapped.detail if mapped.failure_type == FailureType.GROUP_PERMISSION_DENIED.value else TARGET_PERMISSION_DETAIL
            )
            return OperationResult(False, "失败", mapped.failure_type or FailureType.UNKNOWN.value, detail)

    async def _permission_detail_from_probe_exception(self, client: Any, target: Any, mapped: SendResult) -> str:
        if mapped.failure_type == FailureType.GROUP_PERMISSION_DENIED.value and _permission_detail_has_references(mapped.detail or ""):
            return mapped.detail or GROUP_PERMISSION_DETAIL
        context_detail = await self._permission_detail_from_target_context(client, target)
        if context_detail != TARGET_PERMISSION_DETAIL:
            return context_detail
        if mapped.failure_type == FailureType.GROUP_PERMISSION_DENIED.value and mapped.detail:
            return mapped.detail
        return context_detail

    async def _permission_detail_from_target_context(self, client: Any, target: Any) -> str:
        get_messages = getattr(client, "get_messages", None)
        if not callable(get_messages):
            return TARGET_PERMISSION_DETAIL
        try:
            messages = await get_messages(target, limit=VERIFICATION_CONTEXT_DEFAULT_LIMIT)
        except Exception as exc:  # noqa: BLE001 - probe detail should expose context-read failures.
            detail = str(exc) or exc.__class__.__name__
            return f"{TARGET_PERMISSION_DETAIL}；验证上下文读取失败：{detail}"
        rows: list[dict[str, Any]] = []
        for message in messages:
            row = await _verification_context_row(message)
            if row:
                rows.append(row)
        return _permission_detail_from_context_rows(rows)

    async def _update_profile_async(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
        first_name: str,
        last_name: str,
        bio: str,
        avatar_path: str | None,
    ) -> ProfileUpdateResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return ProfileUpdateResult(False, "账号没有可用 session", FailureType.ACCOUNT_UNAVAILABLE.value)
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return ProfileUpdateResult(False, "session 已失效", FailureType.ACCOUNT_UNAVAILABLE.value)
        try:
            from telethon import functions

            await client(
                functions.account.UpdateProfileRequest(
                    first_name=first_name or None,
                    last_name=last_name,
                    about=bio,
                )
            )
            if avatar_path:
                uploaded = await client.upload_file(avatar_path)
                await client(functions.photos.UploadProfilePhotoRequest(file=uploaded))
            return ProfileUpdateResult(True, "profile updated")
        except Exception as exc:  # Keep adapter-level mapping stable for UI/audit.
            mapped = self._map_send_error(exc)
            return ProfileUpdateResult(False, mapped.detail or str(exc), mapped.failure_type or FailureType.UNKNOWN.value)

    def update_profile(
        self,
        session_ciphertext: str | None,
        *,
        first_name: str,
        last_name: str,
        bio: str,
        avatar_path: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> ProfileUpdateResult:
        return self._run(
            self._update_profile_async(
                session_ciphertext,
                self._usable_credentials(credentials),
                first_name,
                last_name,
                bio,
                avatar_path,
            )
        )

    async def _update_profile_photo_async(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
        avatar_path: str,
    ) -> AccountSecurityOperationResult:
        client = await self._authorized_client(session_ciphertext, credentials, error_message="账号没有可用 session")
        if not avatar_path:
            return AccountSecurityOperationResult(False, "失败", "头像文件缺失", "头像文件路径为空")
        try:
            from telethon import functions

            uploaded = await client.upload_file(avatar_path)
            await client(functions.photos.UploadProfilePhotoRequest(file=uploaded))
        except Exception as exc:  # noqa: BLE001 - keep adapter-level mapping stable.
            mapped = self._map_send_error(exc)
            return AccountSecurityOperationResult(False, "失败", mapped.failure_type or FailureType.UNKNOWN.value, mapped.detail or str(exc))
        return AccountSecurityOperationResult(True, "已设置", detail="头像已更新")

    def update_profile_photo(
        self,
        session_ciphertext: str | None,
        avatar_path: str,
        credentials: DeveloperAppCredentials | None = None,
    ) -> AccountSecurityOperationResult:
        return self._run(self._update_profile_photo_async(session_ciphertext, self._usable_credentials(credentials), avatar_path))

    def read_current_authorization(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> AccountAuthorizationSnapshot | None:
        authorizations = self.list_authorizations(session_ciphertext, credentials)
        return next((authorization for authorization in authorizations if authorization.is_current), None)

    async def _pull_profile_async(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
    ) -> RemoteProfile:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            raise RuntimeError("account has no valid session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            raise RuntimeError("session is not authorized")
        me = await client.get_me()
        return RemoteProfile(
            first_name=getattr(me, "first_name", "") or "",
            last_name=getattr(me, "last_name", "") or "",
            bio=getattr(me, "about", "") or "",
            username=getattr(me, "username", None),
        )

    def pull_profile(
        self,
        account_id: int,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> RemoteProfile:
        return self._run(self._pull_profile_async(session_ciphertext, self._usable_credentials(credentials)))

    async def _clone_async(
        self,
        target_type: str,
        peer_id: str,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
    ) -> OperationResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "session 已失效")
        try:
            from telethon import functions

            target: int | str = int(peer_id) if peer_id.lstrip("-").isdigit() else peer_id
            entity = await client.get_entity(target)
            if target_type == "private":
                await client(
                    functions.contacts.AddContactRequest(
                        id=entity,
                        first_name=getattr(entity, "first_name", "") or getattr(entity, "username", None) or "Telegram",
                        last_name=getattr(entity, "last_name", "") or "",
                        phone=getattr(entity, "phone", None) or "",
                    )
                )
                return OperationResult(True, "已完成", detail="联系人已添加")
            if target_type in {"group", "channel"}:
                await client(functions.channels.JoinChannelRequest(channel=entity))
                return OperationResult(True, "已完成", detail="群聊或频道已加入")
            return OperationResult(False, "需人工处理", "不支持的目标类型", "该克隆项需要人工处理")
        except Exception as exc:
            return OperationResult(False, "失败", FailureType.UNKNOWN.value, str(exc) or exc.__class__.__name__)

    def clone_contact_or_group(
        self,
        account_id: int,
        target_type: str,
        peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return self._run(self._clone_async(target_type, peer_id, session_ciphertext, self._usable_credentials(credentials)))

    async def _resolve_verification_async(
        self,
        action: str,
        target_peer_id: str | None,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
    ) -> OperationResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "session 已失效")
        try:
            from telethon import functions

            target: int | str | None = None
            if target_peer_id:
                target = int(target_peer_id) if target_peer_id.lstrip("-").isdigit() else target_peer_id
            if action == "关注频道" and target is not None:
                entity = await client.get_entity(target)
                await client(functions.channels.JoinChannelRequest(channel=entity))
                return OperationResult(True, "已处理", detail="已完成频道关注")
            if action == "点击按钮" and target is not None:
                messages = await client.get_messages(target, limit=VERIFICATION_CONTEXT_DEFAULT_LIMIT)
                message = _first_message_with_buttons(messages)
                if not message or not getattr(message, "buttons", None):
                    return OperationResult(False, "需人工处理", "复杂验证", "未找到可自动点击的按钮")
                click_target = _verification_button_click_target(message)
                if click_target is None:
                    return OperationResult(False, "需人工处理", "复杂验证", "未找到可自动点击的按钮")
                row_index, button_index, label = click_target
                await message.click(row_index, button_index)
                message_id = str(getattr(message, "id", "") or "")
                detail = f"已点击验证按钮 {label} message_id={message_id}".strip()
                return OperationResult(True, "已处理", detail=detail)
            if action == "发送验证回复" and target is not None:
                await client.send_message(target, "/start")
                return OperationResult(True, "已处理", detail="已发送验证回复")
            return OperationResult(False, "需人工处理", "复杂验证", "当前验证需要人工在 Telegram 内处理")
        except Exception as exc:
            return OperationResult(False, "失败", FailureType.UNKNOWN.value, str(exc) or exc.__class__.__name__)

    def resolve_verification_task(
        self,
        account_id: int,
        action: str,
        target_peer_id: str | None = None,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return self._run(
            self._resolve_verification_async(action, target_peer_id, session_ciphertext, self._usable_credentials(credentials))
        )

    async def _fetch_verification_context_async(
        self,
        target_peer_id: str,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            raise RuntimeError("账号没有可用 session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            raise RuntimeError("session 已失效")
        try:
            target = await resolve_telethon_target(client, target_peer_id, group_id=0)
            messages = await client.get_messages(target, limit=limit)
        except Exception as exc:  # noqa: BLE001 - Telegram access failures are runtime API errors.
            detail = str(exc) or exc.__class__.__name__
            raise RuntimeError(f"读取验证聊天失败：{detail}") from exc
        rows: list[dict[str, Any]] = []
        for message in messages:
            row = await _verification_context_row(message)
            if row:
                rows.append(row)
        return rows

    def fetch_verification_context(
        self,
        account_id: int,
        target_peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
        *,
        limit: int = VERIFICATION_CONTEXT_DEFAULT_LIMIT,
    ) -> list[dict[str, Any]]:
        return self._run(
            self._fetch_verification_context_async(target_peer_id, session_ciphertext, self._usable_credentials(credentials), limit=limit)
        )

    async def _fetch_verification_media_async(
        self,
        target_peer_id: str,
        message_id: int,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
    ) -> CachedMediaResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return CachedMediaResult(False, failure_type=FailureType.ACCOUNT_UNAVAILABLE.value, detail="账号没有可用 session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return CachedMediaResult(False, failure_type=FailureType.ACCOUNT_UNAVAILABLE.value, detail="session 已失效")
        try:
            target = await resolve_telethon_target(client, target_peer_id, group_id=0)
            messages = await client.get_messages(target, ids=[message_id])
            message = messages[0] if isinstance(messages, list) else messages
            if not message or not getattr(message, "media", None):
                return CachedMediaResult(False, failure_type="verification_media_missing", detail="未找到验证码图片消息")
            data = await client.download_media(message, bytes)
            if not data:
                return CachedMediaResult(False, failure_type="verification_media_empty", detail="验证码图片下载为空")
            return CachedMediaResult(True, data=bytes(data), detail=_message_media_mime_type(message) or "image/png")
        except Exception as exc:  # noqa: BLE001
            mapped = self._map_send_error(exc)
            return CachedMediaResult(False, failure_type=mapped.failure_type or FailureType.UNKNOWN.value, detail=mapped.detail or str(exc))

    def fetch_verification_media(
        self,
        account_id: int,
        target_peer_id: str,
        message_id: int,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> CachedMediaResult:
        return self._run(
            self._fetch_verification_media_async(target_peer_id, message_id, session_ciphertext, self._usable_credentials(credentials))
        )

    def submit_verification_response(
        self,
        account_id: int,
        target_peer_id: str,
        response_text: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return self.send_message_to_target(account_id, target_peer_id, response_text, "group", None, session_ciphertext, credentials)

    async def _approve_group_verification_messages_async(
        self,
        target_peer_id: str,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
        *,
        bot_name: str,
        limit: int,
    ) -> OperationResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "session 已失效")
        try:
            target = await resolve_telethon_target(client, target_peer_id, group_id=0)
            permissions = await client.get_permissions(target, "me")
            if not _has_group_admin_rights(permissions):
                return OperationResult(False, "需人工处理", "缺少管理员权限", "未找到可执行群验证放行的管理员账号")
            approved = 0
            async for message in client.iter_messages(target, limit=limit):
                if not await _is_group_verification_message(message, bot_name):
                    continue
                if await _click_admin_approve_button(message):
                    approved += 1
            if approved <= 0:
                return OperationResult(False, "需人工处理", "未找到验证按钮", "未找到可点击的通过（管理员）按钮")
            return OperationResult(True, "已处理", detail=f"已点击 {approved} 条通过（管理员）验证")
        except Exception as exc:  # Keep bot-specific behavior behind the adapter boundary.
            mapped = self._map_send_error(exc)
            return OperationResult(False, "失败", mapped.failure_type or FailureType.UNKNOWN.value, mapped.detail or str(exc))

    def approve_group_verification_messages(
        self,
        account_id: int,
        target_peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
        *,
        bot_name: str = "方丈机器人",
        limit: int = 80,
    ) -> OperationResult:
        return self._run(
            self._approve_group_verification_messages_async(
                target_peer_id,
                session_ciphertext,
                self._usable_credentials(credentials),
                bot_name=bot_name,
                limit=limit,
            )
        )

    async def _fetch_group_archive_async(
        self,
        peer_id: str,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
    ) -> ArchiveSnapshot:
        client = await self._authorized_client(session_ciphertext, credentials, error_message="archive fetch requires a valid session")
        return await telethon_content.fetch_group_archive(client, peer_id)

    def fetch_group_archive(
        self,
        account_id: int,
        peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> ArchiveSnapshot:
        return self._run(self._fetch_group_archive_async(peer_id, session_ciphertext, self._usable_credentials(credentials)))

    async def _fetch_group_messages_async(
        self,
        peer_id: str,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
        limit: int,
    ) -> list[GroupMessageSnapshot]:
        client = await self._authorized_client(session_ciphertext, credentials, error_message="listener fetch requires a valid session")
        return await telethon_content.fetch_group_messages(client, peer_id, limit)

    def fetch_group_messages(
        self,
        account_id: int,
        peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
        limit: int = 20,
    ) -> list[GroupMessageSnapshot]:
        return self._run(self._fetch_group_messages_async(peer_id, session_ciphertext, self._usable_credentials(credentials), limit))

    async def _cache_source_media_async(
        self,
        session_ciphertext: str | None,
        source_peer_id: str,
        source_message_id: str,
        cache_peer_id: str,
        credentials: DeveloperAppCredentials,
    ) -> SendResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return SendResult(False, failure_type=FailureType.ACCOUNT_UNAVAILABLE.value, detail="账号没有可用 session")
        if not cache_peer_id:
            return SendResult(False, failure_type="cache_peer_unavailable", detail="缺少源媒体缓存 peer")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return SendResult(False, failure_type=FailureType.ACCOUNT_UNAVAILABLE.value, detail="session 已失效")
        return await telethon_content.cache_source_media(client, source_peer_id, source_message_id, cache_peer_id, self._map_send_error)

    def cache_source_media(
        self,
        account_id: int,
        source_peer_id: str,
        source_message_id: str,
        cache_peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> SendResult:
        return self._run(
            self._cache_source_media_async(
                session_ciphertext,
                source_peer_id,
                source_message_id,
                cache_peer_id,
                self._usable_credentials(credentials),
            )
        )

    async def _cache_material_source_async(
        self,
        session_ciphertext: str | None,
        source: str,
        cache_peer_id: str,
        caption: str,
        credentials: DeveloperAppCredentials,
    ) -> SendResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return SendResult(False, failure_type=FailureType.ACCOUNT_UNAVAILABLE.value, detail="账号没有可用 session")
        if not cache_peer_id:
            return SendResult(False, failure_type="cache_peer_unavailable", detail="缺少素材缓存 peer")
        if not source:
            return SendResult(False, failure_type="cache_not_ready", detail="素材缺少来源")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return SendResult(False, failure_type=FailureType.ACCOUNT_UNAVAILABLE.value, detail="session 已失效")
        return await telethon_content.cache_material_source(client, source, cache_peer_id, caption, self._map_send_error)

    def cache_material_source(
        self,
        account_id: int,
        source: str,
        cache_peer_id: str,
        caption: str = "",
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> SendResult:
        return self._run(
            self._cache_material_source_async(
                session_ciphertext,
                source,
                cache_peer_id,
                caption,
                self._usable_credentials(credentials),
            )
        )

    async def _download_cached_material_async(
        self,
        session_ciphertext: str | None,
        cache_peer_id: str,
        cache_message_id: str,
        credentials: DeveloperAppCredentials,
    ) -> CachedMediaResult:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            return CachedMediaResult(False, failure_type=FailureType.ACCOUNT_UNAVAILABLE.value, detail="账号没有可用 session")
        if not cache_peer_id or not cache_message_id:
            return CachedMediaResult(False, failure_type="cache_media_missing", detail="缺少缓存消息引用")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            return CachedMediaResult(False, failure_type=FailureType.ACCOUNT_UNAVAILABLE.value, detail="session 已失效")
        return await telethon_content.download_cached_material(client, cache_peer_id, cache_message_id, self._map_send_error)

    def download_cached_material(
        self,
        account_id: int,
        cache_peer_id: str,
        cache_message_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> CachedMediaResult:
        return self._run(
            self._download_cached_material_async(
                session_ciphertext,
                cache_peer_id,
                cache_message_id,
                self._usable_credentials(credentials),
            )
        )

    async def _fetch_channel_messages_async(
        self,
        channel_peer_id: str,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
        limit: int,
    ) -> list[ChannelMessageSnapshot]:
        client = await self._authorized_client(session_ciphertext, credentials, error_message="channel message fetch requires a valid session")
        return await telethon_content.fetch_channel_messages(client, channel_peer_id, limit)

    def fetch_channel_messages(
        self,
        account_id: int,
        channel_peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
        limit: int = 20,
    ) -> list[ChannelMessageSnapshot]:
        return self._run(self._fetch_channel_messages_async(channel_peer_id, session_ciphertext, self._usable_credentials(credentials), limit))

    async def _fetch_channel_comments_async(
        self,
        channel_peer_id: str,
        message_id: int,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
        limit: int,
    ) -> list[ChannelCommentSnapshot]:
        client = await self._authorized_client(session_ciphertext, credentials, error_message="channel comment fetch requires a valid session")
        return await telethon_content.fetch_channel_comments(client, channel_peer_id, message_id, limit)

    def fetch_channel_comments(
        self,
        account_id: int,
        channel_peer_id: str,
        message_id: int,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
        limit: int = 100,
    ) -> list[ChannelCommentSnapshot]:
        return self._run(self._fetch_channel_comments_async(channel_peer_id, message_id, session_ciphertext, self._usable_credentials(credentials), limit))


def create_gateway(settings: Settings | None = None) -> TelegramGateway:
    active_settings = settings or get_settings()
    if active_settings.tg_gateway_mode == "telethon":
        return TelethonTelegramGateway(active_settings)
    return TelegramGateway(active_settings)
