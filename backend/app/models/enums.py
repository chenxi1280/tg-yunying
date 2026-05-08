from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum


def now() -> datetime:
    """Return a UTC-naive datetime representing the current time. Used as a default for DB columns."""
    return datetime.now(UTC).replace(tzinfo=None)


class AccountStatus(StrEnum):
    PENDING_LOGIN = "待登录"
    WAITING_CODE = "等待验证码"
    WAITING_QR = "等待扫码"
    WAITING_2FA = "等待2FA"
    ACTIVE = "在线"
    NEED_RELOGIN = "需重新登录"
    LIMITED = "受限"
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


class FailureType(StrEnum):
    ACCOUNT_UNAVAILABLE = "账号不可用"
    ACCOUNT_LIMITED = "账号受限"
    GROUP_PERMISSION_DENIED = "群无权限"
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
]
