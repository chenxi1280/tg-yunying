from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .config import get_settings


@dataclass
class InMemoryTaskQueue:
    _items: deque[int] = field(default_factory=deque)

    def enqueue(self, task_id: int) -> None:
        if task_id not in self._items:
            self._items.append(task_id)

    def dequeue(self) -> int | None:
        if not self._items:
            return None
        return self._items.popleft()

    def size(self) -> int:
        return len(self._items)


class RedisTaskQueue:
    def __init__(self, redis_url: str, key: str = "tg_yunying:message_tasks") -> None:
        try:
            from redis import Redis
        except ImportError as exc:
            raise RuntimeError("redis package is not installed") from exc
        self._redis_url = redis_url
        self.client = Redis.from_url(redis_url, decode_responses=True)
        self.key = key

    def enqueue(self, task_id: int) -> None:
        self.client.lrem(self.key, 0, str(task_id))
        self.client.rpush(self.key, str(task_id))

    def dequeue(self) -> int | None:
        raw = self.client.lpop(self.key)
        return int(raw) if raw else None

    def size(self) -> int:
        return int(self.client.llen(self.key))


_memory_queue = InMemoryTaskQueue()
_redis_queue: RedisTaskQueue | None = None


def get_task_queue():
    settings = get_settings()
    if settings.queue_backend == "redis":
        global _redis_queue
        if _redis_queue is None or _redis_queue._redis_url != settings.redis_url:
            _redis_queue = RedisTaskQueue(settings.redis_url)
        return _redis_queue
    return _memory_queue
