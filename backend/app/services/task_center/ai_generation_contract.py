from __future__ import annotations


AI_GENERATION_UNAVAILABLE_MESSAGE = "AI 生成不可用，等待恢复后继续执行"
GROUP_CHAT_PURPOSE = "群活跃续聊"
GROUP_CHAT_REPLY_PURPOSE = "群引用回复"
CHANNEL_COMMENT_PURPOSE = "频道评论"
CHANNEL_COMMENT_REPLY_PURPOSE = "频道引用回复"
TWO_STAGE_BRIEF_PURPOSE = "两阶段意图规划"
TWO_STAGE_REALIZE_PURPOSE = "两阶段声线实现"
TWO_STAGE_REVIEW_PURPOSE = "两阶段语义审核"
LONG_RUNNING_AI_PURPOSES = frozenset({
    GROUP_CHAT_PURPOSE,
    GROUP_CHAT_REPLY_PURPOSE,
    CHANNEL_COMMENT_PURPOSE,
    CHANNEL_COMMENT_REPLY_PURPOSE,
    TWO_STAGE_BRIEF_PURPOSE,
    TWO_STAGE_REALIZE_PURPOSE,
    TWO_STAGE_REVIEW_PURPOSE,
})


class AiGenerationUnavailable(RuntimeError):
    pass


class ProviderRouteDeferred(AiGenerationUnavailable):
    def __init__(self, detail: str, *, retry_after_seconds: int) -> None:
        self.detail = detail
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        super().__init__(
            f"provider_route_deferred:{detail};retry_after_seconds={self.retry_after_seconds}"
        )
