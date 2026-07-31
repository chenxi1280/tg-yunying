from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class OcrRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=64, max_length=64)
    action_id: str = Field(min_length=1, max_length=128)
    challenge_fingerprint_hash: str = Field(min_length=64, max_length=64)
    image_base64: str = Field(min_length=1)
    mime_type: str
    verification_kind: Literal["math", "alphanumeric"]
    candidate_hash: str = Field(min_length=64, max_length=64)
    deadline_at: datetime
    remaining_budget_ms: int = Field(gt=0)
    contract_version: str = Field(min_length=1, max_length=128)


@dataclass(frozen=True)
class SourceResult:
    source: str
    status: str
    candidates: tuple[dict[str, Any], ...]
    started_at: str
    completed_at: str
    duration_ms: int
    late: bool
    detail: str = ""


@dataclass
class RequestRecord:
    request_id: str
    input_hash: str
    admission_hash: str
    status: str
    worker_instance_id: str
    worker_generation: str
    contract_version: str
    started_at: str
    completed_at: str = ""
    error_code: str = ""
    sources: tuple[SourceResult, ...] = ()
    expires_at: datetime | None = None

    def as_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("expires_at", None)
        payload.pop("admission_hash", None)
        return payload


class WorkerRequestError(RuntimeError):
    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
