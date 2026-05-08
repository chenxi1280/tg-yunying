from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from random import choice, randint
from typing import Any
from uuid import uuid4

from .config import Settings, get_settings
from .models import FailureType
from .security import decrypt_session


@dataclass(frozen=True)
class LoginChallenge:
    status: str
    code_preview: str | None = None
    code_expires_at: datetime | None = None
    qr_payload: str | None = None


@dataclass(frozen=True)
class SendResult:
    ok: bool
    remote_message_id: str | None = None
    failure_type: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class OutboundSegment:
    segment_type: str
    content: str = ""
    source: str | None = None
    caption: str = ""


@dataclass(frozen=True)
class ProfileUpdateResult:
    ok: bool
    detail: str = ""
    failure_type: str | None = None


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    status: str = "已完成"
    failure_type: str = ""
    detail: str = ""


@dataclass(frozen=True)
class GroupSnapshot:
    tg_peer_id: str
    title: str
    group_type: str
    member_count: int
    permission_label: str
    can_send: bool
    slowmode_seconds: int | None = None
    username: str | None = None


@dataclass(frozen=True)
class AccountHealth:
    status: str
    health_score: float
    detail: str


@dataclass(frozen=True)
class VerificationCodeSnapshot:
    code: str
    raw_hint: str
    expires_at: datetime | None


@dataclass(frozen=True)
class ContactSnapshot:
    peer_id: str
    display_name: str
    username: str | None = None
    phone: str | None = None
    contact_type: str = "private"
    is_mutual: bool = False
    last_message_at: datetime | None = None


@dataclass(frozen=True)
class RemoteProfile:
    first_name: str
    last_name: str
    bio: str
    username: str | None = None


@dataclass(frozen=True)
class ArchivedMessageSnapshot:
    sender_name: str
    content: str
    message_type: str = "text"
    sent_at: datetime | None = None


@dataclass(frozen=True)
class GroupMessageSnapshot:
    remote_message_id: str
    sender_name: str
    content: str
    sender_peer_id: str = ""
    message_type: str = "text"
    sent_at: datetime | None = None


@dataclass(frozen=True)
class ArchivedMemberSnapshot:
    display_name: str
    username: str | None = None
    activity_score: int = 0
    tags: str = ""


@dataclass(frozen=True)
class ArchiveSnapshot:
    messages: list[ArchivedMessageSnapshot]
    members: list[ArchivedMemberSnapshot]
    summary: str
    new_group_plan: str


@dataclass(frozen=True)
class DeveloperAppCredentials:
    app_id: int | None
    api_id: int
    api_hash: str
    credentials_version: int
    app_name: str = ""


class TelegramGateway:
    """Adapter boundary for Telethon-backed production integration."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def start_login(
        self,
        method: str,
        account_id: int | None = None,
        phone: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> LoginChallenge:
        if method == "qr":
            return LoginChallenge(
                status="等待扫码",
                qr_payload=f"tg://login?token={uuid4().hex}",
            )
        return LoginChallenge(
            status="等待验证码",
            code_preview=str(randint(10000, 99999)),
            code_expires_at=datetime.now(UTC) + timedelta(seconds=self.settings.login_code_ttl_seconds),
        )

    def finish_login(
        self,
        code: str | None,
        password_2fa: str | None,
        account_id: int | None = None,
        phone: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> tuple[str, str]:
        if password_2fa:
            return "在线", f"encrypted-session:{uuid4().hex}"
        if code == "2fa":
            return "等待2FA", ""
        if code and len(code) >= 4:
            return "在线", f"encrypted-session:{uuid4().hex}"
        return "异常", ""

    def send_message(
        self,
        account_id: int,
        group_id: int,
        content: str,
        segments: list[OutboundSegment] | None = None,
        session_ciphertext: str | None = None,
        peer_id: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> SendResult:
        payload_parts = [content]
        for segment in segments or []:
            payload_parts.extend([segment.content, segment.caption, segment.source or ""])
        payload_text = "\n".join(piece for piece in payload_parts if piece)
        if "违规" in payload_text or "spam" in payload_text.lower():
            return SendResult(False, failure_type=FailureType.CONTENT_REJECTED.value, detail="内容命中禁用词")

        simulated = choice(["ok", "ok", "ok", "slowmode", "limited", "flood"])
        if simulated == "flood":
            return SendResult(False, failure_type=FailureType.FLOOD_WAIT.value, detail="账号触发 FloodWait，建议 120 秒后重试")
        if simulated == "slowmode":
            return SendResult(False, failure_type=FailureType.SLOWMODE.value, detail="目标群启用慢速模式，建议延迟 60 秒")
        if simulated == "limited":
            return SendResult(False, failure_type=FailureType.ACCOUNT_LIMITED.value, detail="账号临时受限，已暂停派单")
        return SendResult(True, remote_message_id=f"tg-{account_id}-{group_id}-{uuid4().hex[:8]}")

    def check_account_health(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> AccountHealth:
        if not session_ciphertext:
            return AccountHealth(status="需重新登录", health_score=45, detail="账号没有可用 session")
        return AccountHealth(status="在线", health_score=95, detail="账号 session 可用")

    def list_groups(
        self,
        account_id: int,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> list[GroupSnapshot]:
        return [
            GroupSnapshot("-100001", "星火项目交流群", "supergroup", 2480, "普通成员", True, None, "spark_group"),
            GroupSnapshot("-100002", "新品内测频道", "channel", 836, "可发帖", True, None, "product_channel"),
            GroupSnapshot("-100003", "海外用户增长群", "supergroup", 1289, "普通成员", True, 30, "growth_group"),
        ]

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

    def view_channel_message(
        self,
        account_id: int,
        channel_peer_id: str,
        message_id: int,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return OperationResult(True, detail=f"viewed:{channel_peer_id}:{message_id}:{account_id}")

    def send_channel_reaction(
        self,
        account_id: int,
        channel_peer_id: str,
        message_id: int,
        reaction: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        if reaction.lower() in {"blocked", "不可用"}:
            return OperationResult(False, "失败", FailureType.REACTION_UNAVAILABLE.value, "Reaction不可用")
        return OperationResult(True, detail=f"reaction:{reaction}:{channel_peer_id}:{message_id}:{account_id}")

    def reply_channel_message(
        self,
        account_id: int,
        channel_peer_id: str,
        message_id: int,
        content: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> SendResult:
        if "无评论" in content:
            return SendResult(False, failure_type=FailureType.COMMENT_UNAVAILABLE.value, detail="频道消息不支持回复")
        return SendResult(True, remote_message_id=f"reply-{account_id}-{message_id}-{uuid4().hex[:8]}")

    def probe_target_capabilities(
        self,
        account_id: int,
        target_peer_id: str,
        target_type: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return OperationResult(True, detail=f"{target_type}:{target_peer_id}:可访问")

    def poll_verification_codes(
        self,
        account_id: int,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> list[VerificationCodeSnapshot]:
        return [
            VerificationCodeSnapshot(
                code=str(randint(10000, 99999)),
                raw_hint="TG 官方服务消息验证码",
                expires_at=datetime.now(UTC) + timedelta(seconds=self.settings.login_code_ttl_seconds),
            )
        ]

    def list_contacts(
        self,
        account_id: int,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> list[ContactSnapshot]:
        return [
            ContactSnapshot(f"mock-user-{account_id}-1", "产品负责人 Alice", "alice_ops", "+8613812345678", "private", True),
            ContactSnapshot(f"mock-user-{account_id}-2", "渠道客户 Bob", "bob_growth", "", "private", False),
            ContactSnapshot(f"mock-user-{account_id}-3", "测试私聊对象", "pytest_target", "", "private", False),
            ContactSnapshot(f"mock-member-{account_id}-1", "星火群友 Leo", "spark_leo", "", "group_member", False),
            ContactSnapshot(f"mock-member-{account_id}-2", "内测群友 Mia", "mimo_mia", "", "group_member", False),
        ]

    def clone_contact_or_group(
        self,
        account_id: int,
        target_type: str,
        peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        if target_type == "private":
            return OperationResult(True, "已完成", detail="mock 联系人已添加")
        if target_type in {"group", "channel"}:
            return OperationResult(True, "已完成", detail="mock 群聊/频道已加入")
        return OperationResult(False, "需人工处理", "不支持的目标类型", "该克隆项需要人工处理")

    def resolve_verification_task(
        self,
        account_id: int,
        action: str,
        target_peer_id: str | None = None,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        if action in {"关注频道", "点击按钮", "发送验证回复"}:
            return OperationResult(True, "已处理", detail=f"mock 已执行：{action}")
        return OperationResult(False, "需人工处理", "复杂验证", "当前验证需要人工在 Telegram 内处理")

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
        if not session_ciphertext:
            return ProfileUpdateResult(False, "账号没有可用 session", FailureType.ACCOUNT_UNAVAILABLE.value)
        return ProfileUpdateResult(True, "mock profile updated")

    def pull_profile(
        self,
        account_id: int,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> RemoteProfile:
        return RemoteProfile(first_name=f"账号{account_id}", last_name="Mock", bio="mock profile", username=f"mock_{account_id}")

    def fetch_group_archive(
        self,
        account_id: int,
        peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> ArchiveSnapshot:
        title = f"群 {peer_id}"
        messages = [
            ArchivedMessageSnapshot(sender_name="活跃成员A", content=f"{title} 最近在讨论使用体验和新手 FAQ。", sent_at=datetime.now(UTC).replace(tzinfo=None)),
            ArchivedMessageSnapshot(sender_name="客服小助手", content="欢迎语和活动规则需要整理成固定话术。", sent_at=datetime.now(UTC).replace(tzinfo=None)),
            ArchivedMessageSnapshot(sender_name="老用户B", content="建议把高频问题和入群引导做成置顶。", sent_at=datetime.now(UTC).replace(tzinfo=None)),
        ]
        members = [
            ArchivedMemberSnapshot(display_name="活跃成员A", username="active_a", activity_score=95, tags="高活跃,可邀请"),
            ArchivedMemberSnapshot(display_name="老用户B", username="senior_b", activity_score=88, tags="高活跃,可邀请"),
            ArchivedMemberSnapshot(display_name="潜在客户C", username="lead_c", activity_score=71, tags="可邀请"),
            ArchivedMemberSnapshot(display_name="内容贡献者D", username="creator_d", activity_score=64, tags="观察"),
        ]
        return ArchiveSnapshot(
            messages=messages,
            members=members,
            summary=f"{title} 的活跃内容集中在欢迎引导、常见问题和真实体验反馈。",
            new_group_plan="新群建议保留欢迎语、FAQ 置顶、前三天轻话题暖场和高活跃成员召回清单。",
        )

    def fetch_group_messages(
        self,
        account_id: int,
        peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
        limit: int = 20,
    ) -> list[GroupMessageSnapshot]:
        now_value = datetime.now(UTC).replace(tzinfo=None)
        return [
            GroupMessageSnapshot(
                remote_message_id=f"mock:{peer_id}:real-user-context",
                sender_peer_id="mock-real-user",
                sender_name="真人用户",
                content=f"这个 {peer_id} 里的话题现在有人在问，具体怎么参与？",
                sent_at=now_value,
            )
        ][:limit]


class TelethonTelegramGateway(TelegramGateway):
    """Telethon-backed production adapter.

    Business services stay synchronous and database-oriented; this adapter owns
    the async Telethon client lifecycle and maps Telegram RPC errors into the
    platform's stable Chinese failure taxonomy.

    Uses a persistent background event loop with a client cache to avoid
    creating a new connection for every operation.
    """

    _loop: asyncio.AbstractEventLoop | None = None
    _loop_thread: threading.Thread | None = None
    _client_cache: dict[tuple[int, str], Any] = {}  # (api_id, session_str) → connected client
    _cache_lock: threading.Lock = threading.Lock()

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        self._pending_clients: dict[int, Any] = {}
        self._pending_qr: dict[int, Any] = {}

    @classmethod
    def _get_or_create_loop(cls) -> asyncio.AbstractEventLoop:
        with cls._cache_lock:
            if cls._loop is None or cls._loop.is_closed():
                cls._loop = asyncio.new_event_loop()
                cls._loop_thread = threading.Thread(target=cls._loop.run_forever, daemon=True)
                cls._loop_thread.start()
            return cls._loop

    @staticmethod
    def _run(coro):
        """Schedule a coroutine on the persistent event loop and block for its result."""
        loop = TelethonTelegramGateway._get_or_create_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=300)

    def _new_client(self, credentials: DeveloperAppCredentials, raw_session: str | None = None) -> Any:
        """Create a fresh, unconnected Telethon client. Used for login flows where the session is not yet established."""
        try:
            from telethon import TelegramClient  # noqa: F811
        except ImportError as exc:
            raise RuntimeError("Telethon package is not installed") from exc
        from telethon.sessions import StringSession

        return TelegramClient(StringSession(raw_session or ""), int(credentials.api_id), credentials.api_hash)

    async def _get_or_create_client(self, credentials: DeveloperAppCredentials, raw_session: str) -> Any:
        """Return a connected Telethon client from the cache, or create and connect a new one."""
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        api_id = int(credentials.api_id)
        cache_key = (api_id, raw_session)
        with self._cache_lock:
            client = self._client_cache.get(cache_key)
        if client is not None:
            try:
                if client.is_connected():
                    return client
            except Exception:
                pass  # stale client, reconnect below
        client = TelegramClient(StringSession(raw_session or ""), api_id, credentials.api_hash)
        await client.connect()
        with self._cache_lock:
            self._client_cache[cache_key] = client
        return client

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
            return LoginChallenge(status="等待扫码", qr_payload=qr_login.url)

        await client.send_code_request(self._usable_phone(phone))
        self._pending_clients[account_id] = client
        return LoginChallenge(
            status="等待验证码",
            code_preview=None,
            code_expires_at=datetime.now(UTC) + timedelta(seconds=self.settings.login_code_ttl_seconds),
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
        try:
            creds = client._self_credentials  # noqa: private access fallback
            api_id = getattr(creds, "api_id", None)
        except Exception:
            api_id = None
        if api_id:
            self._client_cache[(int(api_id), raw_session)] = client
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
        snapshots: list[GroupSnapshot] = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not (dialog.is_group or dialog.is_channel):
                continue
            peer_id = str(getattr(entity, "id", dialog.id))
            if dialog.is_channel:
                peer_id = f"-100{abs(int(peer_id))}"
            default_banned = getattr(entity, "default_banned_rights", None)
            can_send = not bool(default_banned and getattr(default_banned, "send_messages", False))
            snapshots.append(
                GroupSnapshot(
                    tg_peer_id=peer_id,
                    title=dialog.name or "未命名群聊",
                    group_type="channel" if dialog.is_channel and not dialog.is_group else "supergroup" if dialog.is_channel else "group",
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
                        expires_at=datetime.now(UTC) + timedelta(seconds=self.settings.login_code_ttl_seconds),
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
        return SendResult(False, failure_type=FailureType.UNKNOWN.value, detail=str(exc) or exc.__class__.__name__)

    async def _send_async(
        self,
        session_ciphertext: str | None,
        peer_id: str | None,
        content: str,
        segments: list[OutboundSegment] | None,
        credentials: DeveloperAppCredentials,
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
            target: int | str = int(peer_id) if peer_id.lstrip("-").isdigit() else peer_id
            remote_message_id: str | None = None
            if segments:
                for segment in segments:
                    if segment.segment_type == "文本":
                        message = await client.send_message(target, segment.content)
                    elif segment.segment_type == "链接":
                        text = "\n".join(piece for piece in [segment.content, segment.source] if piece).strip()
                        message = await client.send_message(target, text)
                    else:
                        source = segment.source or segment.content
                        if not source:
                            continue
                        try:
                            message = await client.send_file(target, source, caption=segment.caption or segment.content or None)
                        except Exception:
                            fallback_text = "\n".join(piece for piece in [segment.caption or segment.content, source] if piece).strip()
                            message = await client.send_message(target, fallback_text)
                    remote_message_id = str(getattr(message, "id", remote_message_id or uuid4().hex[:8]))
            else:
                message = await client.send_message(target, content)
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
    ) -> SendResult:
        return self._run(self._send_async(session_ciphertext, peer_id, content, segments, self._usable_credentials(credentials)))

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
            message = await client.send_message(entity, content, comment_to=message_id)
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
    ) -> SendResult:
        return self._run(self._reply_channel_message_async(session_ciphertext, channel_peer_id, message_id, content, self._usable_credentials(credentials)))

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
        return OperationResult(True, detail=f"{target_type}:{target_peer_id}:待真实执行时确认权限")

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
                    last_name=last_name or None,
                    about=bio or None,
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
                messages = await client.get_messages(target, limit=1)
                message = messages[0] if messages else None
                if not message or not getattr(message, "buttons", None):
                    return OperationResult(False, "需人工处理", "复杂验证", "未找到可自动点击的按钮")
                await message.click(0)
                return OperationResult(True, "已处理", detail="已点击首个验证按钮")
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

    async def _fetch_group_archive_async(
        self,
        peer_id: str,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials,
    ) -> ArchiveSnapshot:
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            raise RuntimeError("archive fetch requires a valid session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            raise RuntimeError("session is not authorized")
        target: int | str = int(peer_id) if peer_id.lstrip("-").isdigit() else peer_id
        messages_resp = await client.get_messages(target, limit=50)
        messages: list[ArchivedMessageSnapshot] = []
        for message in list(messages_resp or []):
            text = getattr(message, "message", "") or ""
            if not text and not getattr(message, "media", None):
                continue
            sender = await message.get_sender() if hasattr(message, "get_sender") else None
            sender_name = (
                getattr(sender, "first_name", "") or getattr(sender, "title", "") or getattr(sender, "username", None) or "未知成员"
            )
            messages.append(
                ArchivedMessageSnapshot(
                    sender_name=sender_name,
                    content=text or "[media]",
                    message_type="media" if getattr(message, "media", None) else "text",
                    sent_at=getattr(message, "date", None),
                )
            )
        participants: list[ArchivedMemberSnapshot] = []
        counts: dict[str, int] = {}
        for item in messages:
            counts[item.sender_name] = counts.get(item.sender_name, 0) + 1
        async for participant in client.iter_participants(target, limit=80):
            name = f"{getattr(participant, 'first_name', '')} {getattr(participant, 'last_name', '')}".strip() or getattr(participant, "username", None) or str(getattr(participant, "id", ""))
            activity = min(100, counts.get(name, 0) * 20 + (20 if getattr(participant, "username", None) else 0))
            tags = "可邀请" if activity >= 40 else "观察"
            participants.append(
                ArchivedMemberSnapshot(
                    display_name=name,
                    username=getattr(participant, "username", None),
                    activity_score=activity,
                    tags=tags,
                )
            )
        participants.sort(key=lambda item: item.activity_score, reverse=True)
        summary = "群内近期讨论已归档，可继续提炼欢迎语、FAQ 和拉新邀请名单。"
        new_group_plan = "新群建议延续原讨论主题，先铺欢迎语和 FAQ，再召回高活跃成员种子。"
        return ArchiveSnapshot(messages=messages[:50], members=participants[:80], summary=summary, new_group_plan=new_group_plan)

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
        raw_session = decrypt_session(session_ciphertext)
        if not raw_session:
            raise RuntimeError("listener fetch requires a valid session")
        client = await self._get_or_create_client(credentials, raw_session)
        if not await client.is_user_authorized():
            raise RuntimeError("session is not authorized")
        target: int | str = int(peer_id) if peer_id.lstrip("-").isdigit() else peer_id
        messages_resp = await client.get_messages(target, limit=limit)
        snapshots: list[GroupMessageSnapshot] = []
        for message in list(messages_resp or []):
            text = getattr(message, "message", "") or ""
            if not text and not getattr(message, "media", None):
                continue
            sender = await message.get_sender() if hasattr(message, "get_sender") else None
            sender_peer_id = str(getattr(sender, "id", "") or "")
            sender_name = (
                getattr(sender, "first_name", "")
                or getattr(sender, "title", "")
                or getattr(sender, "username", None)
                or sender_peer_id
                or "未知成员"
            )
            snapshots.append(
                GroupMessageSnapshot(
                    remote_message_id=str(getattr(message, "id", uuid4().hex)),
                    sender_peer_id=sender_peer_id,
                    sender_name=sender_name,
                    content=text or "[media]",
                    message_type="media" if getattr(message, "media", None) else "text",
                    sent_at=getattr(message, "date", None),
                )
            )
        return snapshots

    def fetch_group_messages(
        self,
        account_id: int,
        peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
        limit: int = 20,
    ) -> list[GroupMessageSnapshot]:
        return self._run(self._fetch_group_messages_async(peer_id, session_ciphertext, self._usable_credentials(credentials), limit))


def create_gateway(settings: Settings | None = None) -> TelegramGateway:
    active_settings = settings or get_settings()
    if active_settings.tg_gateway_mode == "telethon":
        return TelethonTelegramGateway(active_settings)
    return TelegramGateway(active_settings)
