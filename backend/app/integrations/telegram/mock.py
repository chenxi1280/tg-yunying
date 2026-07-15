from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from random import choice, randint
from uuid import uuid4

from app.config import Settings, get_settings
from .contracts import (
    AccountAuthorizationSnapshot,
    AccountHealth,
    AccountSecurityOperationResult,
    ArchiveSnapshot,
    ArchivedMemberSnapshot,
    ArchivedMessageSnapshot,
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
from app.timezone import BEIJING_TZ, beijing_now


def source_media_hint(*parts: object) -> str:
    return hashlib.sha256("\n".join(str(part or "") for part in parts).encode("utf-8")).hexdigest()


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
            code_expires_at=datetime.now(BEIJING_TZ) + timedelta(seconds=self.settings.login_code_ttl_seconds),
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
        reply_to_message_id: int | None = None,
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

    def check_account_health_isolated(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> AccountHealth:
        return self.check_account_health(session_ciphertext, credentials)

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

    def delete_message(
        self,
        account_id: int,
        target_peer_id: str,
        message_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        if not target_peer_id:
            return OperationResult(False, "失败", FailureType.PEER_INVALID.value, "缺少目标群")
        if not str(message_id).strip():
            return OperationResult(False, "失败", FailureType.PEER_INVALID.value, "缺少消息 ID")
        return OperationResult(True, detail=f"deleted:{target_peer_id}:{message_id}:{account_id}")

    def view_channel_message(
        self,
        account_id: int,
        channel_peer_id: str,
        message_id: int,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return OperationResult(True, detail=f"viewed:{channel_peer_id}:{message_id}:{account_id}")

    def ensure_channel_membership(
        self,
        account_id: int,
        channel_peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
        *,
        invite_link: str = "",
    ) -> ChannelMembershipResult:
        target = channel_peer_id or invite_link
        if "blocked" in target.lower():
            return ChannelMembershipResult(False, "失败", FailureType.PEER_INVALID.value, "频道不可访问", "failed")
        return ChannelMembershipResult(True, detail=f"joined:{target}:{account_id}", membership_status="joined")

    def export_group_invite_link(
        self,
        account_id: int,
        group_peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> InviteLinkResult:
        if "no-admin" in group_peer_id.lower():
            return InviteLinkResult(False, "失败", "admin_required", "账号无权导出邀请链接")
        link = f"https://t.me/+mockInvite{account_id}"
        return InviteLinkResult(True, detail=link, invite_link=link)

    def invite_account_to_group(
        self,
        account_id: int,
        group_peer_id: str,
        target_account_ref: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        if not group_peer_id:
            return OperationResult(False, "失败", FailureType.PEER_INVALID.value, "缺少目标群")
        if not target_account_ref:
            return OperationResult(False, "失败", FailureType.PEER_INVALID.value, "缺少被救援账号")
        if "no-admin" in group_peer_id.lower():
            return OperationResult(False, "失败", FailureType.GROUP_PERMISSION_DENIED.value, "救援账号不是目标群管理员或没有邀请权限")
        return OperationResult(True, "已处理", detail="account_invited")

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
        *,
        reply_to_message_id: int | None = None,
    ) -> SendResult:
        if "无评论" in content:
            return SendResult(False, failure_type=FailureType.COMMENT_UNAVAILABLE.value, detail="频道消息不支持回复")
        target_id = reply_to_message_id or message_id
        return SendResult(True, remote_message_id=f"reply-{account_id}-{target_id}-{uuid4().hex[:8]}")

    def probe_target_capabilities(
        self,
        account_id: int,
        target_peer_id: str,
        target_type: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return OperationResult(True, detail=f"{target_type}:{target_peer_id}:可访问")

    def ensure_linked_channel_membership(
        self,
        account_id: int,
        group_peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return OperationResult(True, "已处理", detail=f"linked-channel:{group_peer_id}:{account_id}")

    def poll_verification_codes(
        self,
        account_id: int,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> list[VerificationCodeSnapshot]:
        return []

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
        if action in {"关注频道", "点击按钮", "发送验证回复", "识别图形验证码"}:
            return OperationResult(True, "已处理", detail=f"mock 已执行：{action}")
        return OperationResult(False, "需人工处理", "复杂验证", "当前验证需要人工在 Telegram 内处理")

    def fetch_verification_context(
        self,
        account_id: int,
        target_peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
        *,
        limit: int = 8,
    ) -> list[dict]:
        return [{
            "message_id": 1,
            "sender": "验证机器人",
            "text": "请输入验证码 1234",
            "sent_at": beijing_now(),
            "has_media": False,
            "media_message_id": None,
            "media_mime_type": "",
            "media_fingerprint": "",
        }]

    def fetch_verification_media(
        self,
        account_id: int,
        target_peer_id: str,
        message_id: int,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> CachedMediaResult:
        return CachedMediaResult(True, data=b"mock-verification-image", detail="image/png")

    def submit_verification_response(
        self,
        account_id: int,
        target_peer_id: str,
        response_text: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        if not response_text.strip():
            return OperationResult(False, "失败", "空验证回复", "请输入验证码或验证回复")
        return OperationResult(True, "已处理", detail="mock 验证回复已发送")

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
        return OperationResult(True, "已处理", detail=f"mock 已通过 {bot_name} 验证消息")

    def lift_group_account_restrictions(
        self,
        account_id: int,
        group_peer_id: str,
        target_account_ref: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return OperationResult(True, "已处理", detail=f"mock 已解除 {target_account_ref} 的群限制")

    def approve_group_join_request(
        self,
        account_id: int,
        group_peer_id: str,
        target_account_ref: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> OperationResult:
        return OperationResult(True, "已处理", detail=f"mock 已审批 {target_account_ref} 的入群申请")

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

    def list_authorizations(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> list[AccountAuthorizationSnapshot]:
        if not session_ciphertext:
            return []
        now_value = beijing_now()
        app_name = credentials.app_name if credentials else "TG运营平台"
        api_id = credentials.api_id if credentials else 0
        return [
            AccountAuthorizationSnapshot(
                authorization_hash="current-platform-session",
                is_current=True,
                device_model="TG运营平台-主控",
                platform="server",
                system_version="Linux",
                api_id=api_id,
                app_name=app_name,
                app_version="1.0",
                ip="10.0.0.8",
                country="CN",
                region="平台",
                date_created=now_value - timedelta(days=2),
                date_active=now_value,
            ),
            AccountAuthorizationSnapshot(
                authorization_hash="external-mobile-session",
                is_current=False,
                device_model="iPhone",
                platform="iOS",
                system_version="17",
                api_id=api_id,
                app_name="Telegram",
                app_version="10.0",
                ip="203.0.113.88",
                country="CN",
                region="外部",
                date_created=now_value - timedelta(days=15),
                date_active=now_value - timedelta(hours=3),
            ),
        ]

    def cleanup_authorization(
        self,
        session_ciphertext: str | None,
        authorization_hash: str,
        credentials: DeveloperAppCredentials | None = None,
    ) -> AccountSecurityOperationResult:
        if not session_ciphertext:
            return AccountSecurityOperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        if authorization_hash == "current-platform-session":
            return AccountSecurityOperationResult(False, "失败", "当前平台Session受保护", "不能退出当前平台可信设备")
        return AccountSecurityOperationResult(True, "已清理", detail=f"mock cleaned {authorization_hash}")

    def get_two_fa_status(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> AccountSecurityOperationResult:
        if not session_ciphertext:
            return AccountSecurityOperationResult(False, "unknown", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        return AccountSecurityOperationResult(True, "missing", detail="mock 2FA missing")

    def set_two_fa_password(
        self,
        session_ciphertext: str | None,
        password: str,
        hint: str = "",
        recovery_email: str = "",
        credentials: DeveloperAppCredentials | None = None,
        current_password: str | None = None,
    ) -> AccountSecurityOperationResult:
        if not session_ciphertext:
            return AccountSecurityOperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        if recovery_email and recovery_email.endswith("@confirm.test"):
            return AccountSecurityOperationResult(True, "pending_email_confirmation", detail="mock waiting email confirmation")
        return AccountSecurityOperationResult(True, "enabled", detail="mock 2FA enabled")

    def confirm_two_fa_email(
        self,
        session_ciphertext: str | None,
        code: str,
        credentials: DeveloperAppCredentials | None = None,
    ) -> AccountSecurityOperationResult:
        if not session_ciphertext:
            return AccountSecurityOperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        if not code:
            return AccountSecurityOperationResult(False, "失败", "邮箱验证码缺失", "请输入 Telegram 恢复邮箱验证码")
        return AccountSecurityOperationResult(True, "enabled", detail="mock 2FA email confirmed")

    def update_username(
        self,
        session_ciphertext: str | None,
        username: str,
        credentials: DeveloperAppCredentials | None = None,
    ) -> AccountSecurityOperationResult:
        if not session_ciphertext:
            return AccountSecurityOperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        if "taken" in username.lower():
            return AccountSecurityOperationResult(False, "失败", "用户名被占用", f"{username} 已被占用")
        return AccountSecurityOperationResult(True, "已设置", detail=username)

    def update_profile_photo(
        self,
        session_ciphertext: str | None,
        avatar_path: str,
        credentials: DeveloperAppCredentials | None = None,
    ) -> AccountSecurityOperationResult:
        if not session_ciphertext:
            return AccountSecurityOperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "账号没有可用 session")
        if not avatar_path:
            return AccountSecurityOperationResult(False, "失败", "头像文件缺失", "头像文件路径为空")
        return AccountSecurityOperationResult(True, "已设置", detail="mock profile photo updated")

    def read_current_authorization(
        self,
        session_ciphertext: str | None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> AccountAuthorizationSnapshot | None:
        return next((authorization for authorization in self.list_authorizations(session_ciphertext, credentials) if authorization.is_current), None)

    def fetch_group_archive(
        self,
        account_id: int,
        peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> ArchiveSnapshot:
        title = f"群 {peer_id}"
        messages = [
            ArchivedMessageSnapshot(sender_name="活跃成员A", sender_phone="+8613811110001", content=f"{title} 最近在讨论使用体验和新手 FAQ。", sent_at=beijing_now()),
            ArchivedMessageSnapshot(sender_name="客服小助手", content="欢迎语和活动规则需要整理成固定话术。", sent_at=beijing_now()),
            ArchivedMessageSnapshot(sender_name="老用户B", sender_phone="+8613811110002", content="建议把高频问题和入群引导做成置顶。", sent_at=beijing_now()),
        ]
        members = [
            ArchivedMemberSnapshot(display_name="活跃成员A", username="active_a", phone="+8613811110001", activity_score=95, tags="高活跃,可邀请"),
            ArchivedMemberSnapshot(display_name="老用户B", username="senior_b", phone="+8613811110002", activity_score=88, tags="高活跃,可邀请"),
            ArchivedMemberSnapshot(display_name="潜在客户C", username="lead_c", phone="+8613811110003", activity_score=71, tags="可邀请"),
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
        now_value = beijing_now()
        return [
            GroupMessageSnapshot(
                remote_message_id=f"mock:{peer_id}:real-user-context",
                sender_peer_id="mock-real-user",
                sender_name="真人用户",
                content=f"这个 {peer_id} 里的话题现在有人在问，具体怎么参与？",
                sent_at=now_value,
            )
        ][:limit]

    def cache_source_media(
        self,
        account_id: int,
        source_peer_id: str,
        source_message_id: str,
        cache_peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> SendResult:
        if not cache_peer_id:
            return SendResult(False, failure_type="cache_peer_unavailable", detail="缺少源媒体缓存 peer")
        return SendResult(True, remote_message_id=f"cache:{source_peer_id}:{source_message_id}")

    def cache_material_source(
        self,
        account_id: int,
        source: str,
        cache_peer_id: str,
        caption: str = "",
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> SendResult:
        if not cache_peer_id:
            return SendResult(False, failure_type="cache_peer_unavailable", detail="缺少素材缓存 peer")
        if not source:
            return SendResult(False, failure_type="cache_not_ready", detail="素材缺少来源")
        return SendResult(True, remote_message_id=f"material-cache:{uuid4().hex[:8]}")

    def download_cached_material(
        self,
        account_id: int,
        cache_peer_id: str,
        cache_message_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
    ) -> CachedMediaResult:
        if not session_ciphertext:
            return CachedMediaResult(False, failure_type=FailureType.ACCOUNT_UNAVAILABLE.value, detail="账号没有可用 session")
        if not cache_peer_id or not cache_message_id:
            return CachedMediaResult(False, failure_type="cache_media_missing", detail="缺少缓存消息引用")
        return CachedMediaResult(True, data=b"\x89PNG\r\n\x1a\nmock-cached-avatar")

    def fetch_channel_messages(
        self,
        account_id: int,
        channel_peer_id: str,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
        limit: int = 20,
    ) -> list[ChannelMessageSnapshot]:
        now_value = beijing_now()
        return [
            ChannelMessageSnapshot(
                message_id=100000 + index,
                content_preview=f"mock channel {channel_peer_id} message {index + 1}",
                message_url="",
                published_at=now_value - timedelta(minutes=index),
            )
            for index in range(max(1, limit))
        ][:limit]

    def fetch_channel_comments(
        self,
        account_id: int,
        channel_peer_id: str,
        message_id: int,
        session_ciphertext: str | None = None,
        credentials: DeveloperAppCredentials | None = None,
        limit: int = 100,
    ) -> list[ChannelCommentSnapshot]:
        return []
