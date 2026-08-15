from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class GenerationDependencies:
    normal_generator: Callable
    reply_generator: Callable
    reply_target_probe: Callable
    reply_message_fetcher: Callable
    # 两阶段生成（PRD §5.4）注入点；None 时使用 two_stage_generation 默认通道。
    brief_planner: Callable | None = None
    brief_realizer: Callable | None = None
    semantic_reviewer: Callable | None = None


__all__ = ["GenerationDependencies"]
