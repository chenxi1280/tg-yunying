from dataclasses import dataclass


DEFAULT_RESOURCE_RETRY_SECONDS = 30


# Exception tracebacks are assigned by Python transaction context managers.
@dataclass
class RuntimeResourceBlocked(Exception):
    code: str
    detail: str
    retry_after_seconds: int = DEFAULT_RESOURCE_RETRY_SECONDS
