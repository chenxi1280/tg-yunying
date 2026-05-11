from __future__ import annotations

from datetime import datetime, timedelta
from threading import Lock

from app.services._common import _now


_LOCK = Lock()
_RECENT_COLLECTS: dict[tuple[str, int], datetime] = {}


def should_collect_listener(object_type: str, object_id: int, *, window_seconds: int = 30) -> bool:
    key = (object_type, int(object_id))
    now_value = _now()
    window = timedelta(seconds=max(1, int(window_seconds or 30)))
    with _LOCK:
        last_collect = _RECENT_COLLECTS.get(key)
        if last_collect and last_collect + window > now_value:
            return False
        _RECENT_COLLECTS[key] = now_value
        return True


def invalidate_listener_collect(object_type: str, object_id: int) -> None:
    key = (object_type, int(object_id))
    with _LOCK:
        _RECENT_COLLECTS.pop(key, None)


def reset_listener_runtime_cache() -> None:
    with _LOCK:
        _RECENT_COLLECTS.clear()


__all__ = ["invalidate_listener_collect", "reset_listener_runtime_cache", "should_collect_listener"]
