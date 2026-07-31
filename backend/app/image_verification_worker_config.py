from __future__ import annotations

import os
from dataclasses import dataclass

PROC_SELF_STATUS_PATH = "/proc/self/status"
RSS_LINE_PREFIX = "VmRSS:"
KIBIBYTE_BYTES = 1024


@dataclass(frozen=True)
class WorkerConfig:
    token: str
    contract_version: str
    max_image_bytes: int
    max_image_pixels: int
    max_dimension: int
    max_budget_seconds: float
    recovery_observation_seconds: float
    terminal_ttl_seconds: float
    recycle_request_limit: int
    soft_rss_bytes: int

    def __post_init__(self) -> None:
        minimum_ttl = (
            self.max_budget_seconds + self.recovery_observation_seconds
        )
        if self.terminal_ttl_seconds < minimum_ttl:
            raise ValueError(
                "IMAGE_VERIFICATION_TERMINAL_TTL_SECONDS must be at least "
                "max budget plus recovery observation seconds"
            )

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        return cls(
            token=_required_env("IMAGE_VERIFICATION_WORKER_TOKEN"),
            contract_version=_required_env(
                "IMAGE_VERIFICATION_CONTRACT_VERSION"
            ),
            max_image_bytes=_positive_int_env(
                "IMAGE_VERIFICATION_MAX_IMAGE_BYTES"
            ),
            max_image_pixels=_positive_int_env(
                "IMAGE_VERIFICATION_MAX_IMAGE_PIXELS"
            ),
            max_dimension=_positive_int_env(
                "IMAGE_VERIFICATION_MAX_IMAGE_DIMENSION"
            ),
            max_budget_seconds=_positive_float_env(
                "IMAGE_VERIFICATION_WORKER_MAX_BUDGET_SECONDS"
            ),
            recovery_observation_seconds=_positive_float_env(
                "IMAGE_VERIFICATION_RECOVERY_OBSERVATION_SECONDS"
            ),
            terminal_ttl_seconds=_positive_float_env(
                "IMAGE_VERIFICATION_TERMINAL_TTL_SECONDS"
            ),
            recycle_request_limit=_positive_int_env(
                "IMAGE_VERIFICATION_WORKER_RECYCLE_REQUEST_LIMIT"
            ),
            soft_rss_bytes=_positive_int_env(
                "IMAGE_VERIFICATION_WORKER_SOFT_RSS_BYTES"
            ),
        )


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _positive_int_env(name: str) -> int:
    value = int(_required_env(name))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _positive_float_env(name: str) -> float:
    value = float(_required_env(name))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def current_worker_rss_bytes() -> int:
    try:
        with open(PROC_SELF_STATUS_PATH, encoding="utf-8") as status_file:
            for line in status_file:
                if line.startswith(RSS_LINE_PREFIX):
                    return int(line.split()[1]) * KIBIBYTE_BYTES
    except (OSError, ValueError, IndexError):
        return 0
    return 0
