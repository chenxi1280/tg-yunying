from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from app.timezone import beijing_now


def now() -> datetime:
    """Return a UTC-naive datetime whose wall-clock value is Beijing time."""
    return beijing_now()


class AccountStatus(StrEnum):
    PENDING_LOGIN = "待登录"
    WAITING_CODE = "等待验证码"
    WAITING_QR = "等待扫码"
    WAITING_2FA = "等待2FA"
    ACTIVE = "在线"
    NEED_RELOGIN = "需重新登录"
    LIMITED = "受限"
    SESSION_EXPIRED = "Session失效"
    SUSPECTED_BANNED = "疑似封禁"
    BANNED = "已封禁"
    DISABLED = "禁用"
    ERROR = "异常"


class GroupAuthStatus(StrEnum):
    UNVERIFIED = "未确认"
    AUTHORIZED = "已授权运营"
    READONLY = "只读归档"
    BLOCKED = "禁止操作"


class TaskStatus(StrEnum):
    DRAFT = "草稿"
    PENDING_REVIEW = "待审核"
    APPROVED = "已审核"
    QUEUED = "排队中"
    RUNNING = "执行中"
    SENDING = "发送中"
    SENT = "已发送"
    FAILED = "失败"
    PAUSED = "已暂停"
    CANCELLED = "已取消"
    REJECTED = "已驳回"
    COMPLETED = "已完成"


class TaskCenterTaskType(StrEnum):
    GROUP_AI_CHAT = "group_ai_chat"
    GROUP_RELAY = "group_relay"
    GROUP_MEMBERSHIP_ADMISSION = "group_membership_admission"
    CHANNEL_VIEW = "channel_view"
    CHANNEL_LIKE = "channel_like"
    CHANNEL_COMMENT = "channel_comment"


class TaskCenterStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskCenterActionType(StrEnum):
    SEND_MESSAGE = "send_message"
    VIEW_MESSAGE = "view_message"
    LIKE_MESSAGE = "like_message"
    POST_COMMENT = "post_comment"
    INVITE_GROUP_BOT = "invite_group_bot"
    INVITE_GROUP_ACCOUNT = "invite_group_account"
    ENSURE_TARGET_MEMBERSHIP = "ensure_target_membership"
    ENSURE_CHANNEL_MEMBERSHIP = "ensure_channel_membership"


class TaskCenterActionStatus(StrEnum):
    PENDING = "pending"
    EXECUTING = "executing"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskCenterReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class FailureType(StrEnum):
    ACCOUNT_UNAVAILABLE = "账号不可用"
    ACCOUNT_LIMITED = "账号受限"
    GROUP_PERMISSION_DENIED = "群无权限"
    CHANNEL_POST_DENIED = "频道无发帖权限"
    COMMENT_UNAVAILABLE = "评论区不可用"
    REACTION_UNAVAILABLE = "Reaction不可用"
    SLOWMODE = "群慢速模式"
    FLOOD_WAIT = "FloodWait"
    CONTENT_REJECTED = "内容违规"
    PEER_INVALID = "目标无效"
    UNKNOWN = "未知错误"


class DeveloperAppHealthStatus(StrEnum):
    HEALTHY = "健康"
    UNHEALTHY = "异常"
    DISABLED = "禁用"


class AiProviderHealthStatus(StrEnum):
    HEALTHY = "健康"
    UNHEALTHY = "异常"
    DISABLED = "禁用"


__all__ = [
    "now",
    "AccountStatus",
    "AiProviderHealthStatus",
    "DeveloperAppHealthStatus",
    "FailureType",
    "GroupAuthStatus",
    "TaskStatus",
    "TaskCenterActionStatus",
    "TaskCenterActionType",
    "TaskCenterReviewStatus",
    "TaskCenterStatus",
    "TaskCenterTaskType",
]
