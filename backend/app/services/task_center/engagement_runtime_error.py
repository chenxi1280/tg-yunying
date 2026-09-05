from dataclasses import dataclass


DEFAULT_RESOURCE_RETRY_SECONDS = 30


@dataclass(frozen=True)
class RuntimeResourceBlocked(Exception):
    code: str
    detail: str
    retry_after_seconds: int = DEFAULT_RESOURCE_RETRY_SECONDS
