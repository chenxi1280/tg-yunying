from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.integrations.telegram.search_join import ImageVerificationRequest

from .image_verification_runtime import (
    ImageVerificationPolicy,
    VerificationBudget,
)


class RemoteOcrError(RuntimeError):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        worker_generation: str = "",
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.worker_generation = worker_generation


@dataclass(frozen=True)
class RemoteOcrSource:
    source: str
    status: str
    candidates: tuple[tuple[str, float], ...]
    started_at: str
    completed_at: str
    duration_ms: int
    late: bool
    detail: str


@dataclass(frozen=True)
class RemoteOcrResult:
    request_id: str
    input_hash: str
    worker_generation: str
    sources: tuple[RemoteOcrSource, ...]


def deterministic_request_id(
    action_id: str,
    challenge_fingerprint_hash: str,
    contract_version: str,
) -> str:
    value = "|".join(
        (action_id, challenge_fingerprint_hash, contract_version)
    )
    return hashlib.sha256(value.encode()).hexdigest()


class ImageVerificationWorkerClient:
    def __init__(self, policy: ImageVerificationPolicy) -> None:
        self._policy = policy

    def recognize(
        self,
        action_id: str,
        request: ImageVerificationRequest,
        budget: VerificationBudget,
    ) -> RemoteOcrResult:
        request_id = deterministic_request_id(
            action_id,
            request.challenge_fingerprint_hash,
            self._policy.contract_version,
        )
        payload = self._request_payload(
            request_id,
            action_id,
            request,
            budget,
        )
        try:
            response = self._request_json(
                "POST",
                "/internal/v1/image-verification/ocr",
                payload,
                budget.remaining_seconds(),
            )
        except (TimeoutError, urllib.error.URLError):
            response = self._status_after_timeout(request_id, budget)
        except RemoteOcrError as exc:
            if exc.code != "verification_local_ocr_timeout":
                raise
            response = self._status_after_timeout(request_id, budget)
        return _parse_completed_result(
            response,
            expected_request_id=request_id,
            expected_contract_version=self._policy.contract_version,
        )

    def _status_after_timeout(
        self,
        request_id: str,
        budget: VerificationBudget,
    ) -> dict[str, Any]:
        if budget.remaining_seconds() <= 0:
            raise RemoteOcrError(
                "verification_local_ocr_timeout",
                "OCR POST timed out and no query budget remains",
            )
        try:
            status = self._request_json(
                "GET",
                f"/internal/v1/image-verification/ocr/{request_id}",
                None,
                budget.remaining_seconds(),
            )
        except (TimeoutError, urllib.error.URLError) as exc:
            raise RemoteOcrError(
                "verification_local_ocr_timeout",
                "OCR status query timed out; duplicate POST is forbidden",
            ) from exc
        _validate_response_identity(
            status,
            expected_request_id=request_id,
            expected_contract_version=self._policy.contract_version,
        )
        state = str(status.get("status") or "unknown")
        if state == "running":
            raise RemoteOcrError(
                "verification_local_ocr_timeout",
                "OCR request is still running; duplicate POST is forbidden",
                worker_generation=str(status.get("worker_generation") or ""),
            )
        if state == "unknown":
            raise RemoteOcrError(
                "verification_local_ocr_unknown",
                "OCR worker generation cannot prove prior request state",
                worker_generation=str(status.get("worker_generation") or ""),
            )
        return status

    def _request_payload(
        self,
        request_id: str,
        action_id: str,
        request: ImageVerificationRequest,
        budget: VerificationBudget,
    ) -> dict[str, Any]:
        return {
            "request_id": request_id,
            "action_id": action_id,
            "challenge_fingerprint_hash": request.challenge_fingerprint_hash,
            "image_base64": base64.b64encode(request.image_bytes).decode("ascii"),
            "mime_type": request.mime_type,
            "verification_kind": (
                "math" if _is_math_request(request) else "alphanumeric"
            ),
            "candidate_hash": request.candidate_hash,
            "deadline_at": budget.callback_deadline_at.isoformat(),
            "remaining_budget_ms": max(
                1,
                int(budget.remaining_seconds() * 1000),
            ),
            "contract_version": self._policy.contract_version,
        }

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self._policy.worker_url}{path}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Internal-Token": self._policy.worker_token,
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return _decode_json_response(response.read())
        except urllib.error.HTTPError as exc:
            detail = _http_error_detail(exc)
            raise RemoteOcrError(
                detail["code"],
                detail["detail"],
            ) from exc


def _parse_completed_result(
    payload: dict[str, Any],
    *,
    expected_request_id: str,
    expected_contract_version: str,
) -> RemoteOcrResult:
    _validate_response_identity(
        payload,
        expected_request_id=expected_request_id,
        expected_contract_version=expected_contract_version,
    )
    status = str(payload.get("status") or "unknown")
    if status != "completed":
        raise RemoteOcrError(
            _remote_status_error_code(payload, status),
            f"OCR worker returned terminal status {status}",
            worker_generation=str(payload.get("worker_generation") or ""),
        )
    sources = _parse_sources(payload.get("sources"))
    input_hash = str(payload.get("input_hash") or "")
    worker_generation = str(payload.get("worker_generation") or "")
    if len(input_hash) != 64 or not worker_generation:
        raise _contract_error("OCR response terminal identity is incomplete")
    return RemoteOcrResult(
        request_id=expected_request_id,
        input_hash=input_hash,
        worker_generation=worker_generation,
        sources=sources,
    )


def _remote_status_error_code(
    payload: dict[str, Any],
    status: str,
) -> str:
    explicit = str(payload.get("error_code") or "")
    if explicit:
        return explicit
    return {
        "running": "verification_local_ocr_timeout",
        "expired": "verification_deadline_exceeded",
        "unknown": "verification_local_ocr_unknown",
    }.get(status, "verification_local_ocr_unavailable")


def _parse_sources(value: Any) -> tuple[RemoteOcrSource, ...]:
    if not isinstance(value, list) or len(value) != 2:
        raise _contract_error("OCR response must contain exactly two sources")
    sources = tuple(_parse_source(item) for item in value)
    if {source.source for source in sources} != {"rapidocr", "ddddocr"}:
        raise _contract_error("OCR response source set is invalid")
    return sources


def _parse_source(payload: Any) -> RemoteOcrSource:
    if not isinstance(payload, dict):
        raise _contract_error("OCR source payload must be an object")
    try:
        source = str(payload["source"])
        status = str(payload["status"])
        candidates = _parse_candidates(payload["candidates"])
        started_at = str(payload["started_at"])
        completed_at = str(payload["completed_at"])
        duration_ms = int(payload["duration_ms"])
        late = payload["late"]
    except (KeyError, TypeError, ValueError) as exc:
        raise _contract_error("OCR source payload is malformed") from exc
    if status not in {"complete", "failed"} or duration_ms < 0:
        raise _contract_error("OCR source status or duration is invalid")
    if not isinstance(late, bool) or not started_at or not completed_at:
        raise _contract_error("OCR source timing payload is invalid")
    return RemoteOcrSource(
        source=source,
        status=status,
        candidates=candidates,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        late=late,
        detail=str(payload.get("detail") or ""),
    )


def _parse_candidates(value: Any) -> tuple[tuple[str, float], ...]:
    if not isinstance(value, list):
        raise _contract_error("OCR source candidates must be a list")
    try:
        candidates = tuple(
            (str(candidate["text"]), float(candidate["confidence"]))
            for candidate in value
            if isinstance(candidate, dict)
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _contract_error("OCR candidate payload is malformed") from exc
    if len(candidates) != len(value):
        raise _contract_error("OCR candidate payload must be an object")
    if any(not 0 <= confidence <= 1 for _, confidence in candidates):
        raise _contract_error("OCR candidate confidence is invalid")
    return candidates


def _validate_response_identity(
    payload: Any,
    *,
    expected_request_id: str,
    expected_contract_version: str,
) -> None:
    if not isinstance(payload, dict):
        raise _contract_error("OCR response must be an object")
    if str(payload.get("request_id") or "") != expected_request_id:
        raise _contract_error("OCR response request_id does not match request")
    if str(payload.get("contract_version") or "") != expected_contract_version:
        raise _contract_error("OCR response contract_version does not match request")


def _decode_json_response(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RemoteOcrError(
            "verification_local_ocr_unavailable",
            "OCR worker returned malformed JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise _contract_error("OCR response must be an object")
    return payload


def _contract_error(detail: str) -> RemoteOcrError:
    return RemoteOcrError("verification_contract_mismatch", detail)


def _http_error_detail(exc: urllib.error.HTTPError) -> dict[str, str]:
    try:
        payload = json.loads(exc.read().decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {
            "code": "verification_local_ocr_unavailable",
            "detail": f"OCR worker HTTP {exc.code}",
        }
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if not isinstance(detail, dict):
        return {
            "code": "verification_local_ocr_unavailable",
            "detail": str(detail or f"OCR worker HTTP {exc.code}"),
        }
    return {
        "code": str(detail.get("code") or "verification_local_ocr_unavailable"),
        "detail": str(detail.get("detail") or f"OCR worker HTTP {exc.code}"),
    }


def _is_math_request(request: ImageVerificationRequest) -> bool:
    return bool(request.candidate_answers) and all(
        answer.isdigit() for answer in request.candidate_answers
    )
