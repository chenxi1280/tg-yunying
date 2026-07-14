from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class GenerationDependencies:
    normal_generator: Callable
    reply_generator: Callable
    reply_target_probe: Callable
    reply_messages_fetcher: Callable


__all__ = ["GenerationDependencies"]
