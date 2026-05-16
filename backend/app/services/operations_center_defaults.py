from __future__ import annotations

from typing import Any

from app.schemas.operations_center import RuleSummaryOut

ACTIVE_TASK_STATUSES = {"draft", "pending", "running", "paused"}
LISTENER_TASK_STATUSES = {"pending", "running"}
DEFAULT_RULE_SET_NAME = "默认运营规则集"
LEGACY_DEFAULT_RELAY_RULE_SET_NAME = "默认转发监听过滤规则"
DEFAULT_RULE_SET_DESCRIPTION = "系统初始化的通用规则集，默认不拦截内容，可用于监听转发、AI 回复、AI 评论和普通消息发送。"
DEFAULT_RULE_TASK_TYPES = ["group_relay", "group_ai_chat", "channel_comment", "message_send"]
DEFAULT_RELAY_FILTERS = {
    "keyword_whitelist": [],
    "keyword_blacklist": [],
    "min_message_length": None,
    "max_message_length": None,
    "allowed_media_types": [],
    "blocked_user_ids": [],
    "only_with_media": False,
    "only_text": False,
    "language_filter": None,
}
DEFAULT_RELAY_OUTPUT_CHECKS = {
    "forbidden_keywords": [],
    "forbid_links": False,
    "forbid_mentions": True,
    "max_length": None,
    "failure_strategy": "transform_once_drop",
}


def _default_relay_output_checks() -> dict[str, Any]:
    return {
        key: list(value) if isinstance(value, list) else value
        for key, value in DEFAULT_RELAY_OUTPUT_CHECKS.items()
    }


def _default_relay_filters() -> dict[str, Any]:
    return {
        key: list(value) if isinstance(value, list) else value
        for key, value in DEFAULT_RELAY_FILTERS.items()
    }

SYSTEM_RULES = [
    RuleSummaryOut(
        key="auto-validation",
        category="自动校验",
        name="AI 内容发送前校验",
        status="已启用",
        detail="空内容、敏感词、重复内容、长度、外链、@ 成员、账号冷却和目标频控检查",
        version="system",
    ),
    RuleSummaryOut(
        key="relay-routing",
        category="路由策略",
        name="转发监听自动路由",
        status="已启用",
        detail="源消息先过滤和转换，再按任务目标群与账号策略生成发送项",
        version="system",
    ),
    RuleSummaryOut(
        key="sticky-account",
        category="发送账号策略",
        name="目标粘性账号优先",
        status="已启用",
        detail="账号可重复发送，但受冷却、每日上限、目标群连续发送限制约束",
        version="system",
    ),
    RuleSummaryOut(
        key="retry-policy",
        category="失败处理",
        name="失败重试与跳过策略",
        status="已启用",
        detail="失败项可重试，账号不可用、内容拦截、上下文过期等原因保留在执行记录",
        version="system",
    ),
]
