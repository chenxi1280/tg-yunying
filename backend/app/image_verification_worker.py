from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import signal
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from io import BytesIO
from time import monotonic
from typing import Any, Callable
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from PIL import Image

from app import image_verification_ocr
from app.image_verification_worker_contract import (
    OcrRequest,
    RequestRecord,
    SourceResult,
    WorkerRequestError,
)
from app.image_verification_worker_config import (
    WorkerConfig,
    current_worker_rss_bytes,
)

ALLOWED_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
BASE64_EXPANSION_RATIO = 4 / 3
TERMINATION_DELAY_SECONDS = 0.1


class ImageVerificationWorkerService:
    def __init__(
        self,
        config: WorkerConfig,
        *,
        abnormal_terminate: Callable[[], None] | None = None,
        rss_reader: Callable[[], int] | None = None,
    ) -> None:
        self.config = config
        self.instance_id = str(uuid4())
        self.generation = str(uuid4())
        self._lock = threading.Lock()
        self._records: dict[str, RequestRecord] = {}
        self._active_request_id = ""
        self._completed_requests = 0
        self._busy_rejections = 0
        self._rapidocr = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="worker-rapidocr",
        )
        self._ddddocr = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="worker-ddddocr",
        )
        self._abnormal_terminate = (
            abnormal_terminate or _schedule_abnormal_termination
        )
        self._rss_reader = rss_reader or current_worker_rss_bytes

    def execute(self, request: OcrRequest) -> tuple[dict[str, Any], bool]:
        self._validate_contract(request)
        admission_hash = _admission_hash(request)
        try:
            budget = _request_budget_seconds(request, self.config)
        except WorkerRequestError as exc:
            existing = self._begin_request(request, admission_hash)
            if existing is not None:
                return existing.as_payload(), self._should_recycle()
            self._complete_request(
                request.request_id,
                "expired",
                (),
                error_code=exc.code,
            )
            raise
        existing = self._begin_request(request, admission_hash)
        if existing is not None:
            return existing.as_payload(), self._should_recycle()
        try:
            image_bytes, input_hash = self._validate_request(request)
        except WorkerRequestError as exc:
            self._complete_request(
                request.request_id,
                "failed",
                (),
                error_code=exc.code,
            )
            raise
        self._set_input_hash(request.request_id, input_hash)
        deadline_monotonic = monotonic() + budget
        futures = self._submit_sources(image_bytes, deadline_monotonic)
        _, pending = wait(
            futures,
            timeout=max(0.0, deadline_monotonic - monotonic()),
        )
        if pending:
            self._abnormal_terminate()
            raise WorkerRequestError(
                504,
                "verification_local_ocr_timeout",
                "native OCR exceeded request deadline; generation will exit",
            )
        sources = tuple(future.result() for future in futures)
        status = "completed" if any(
            source.status == "complete" for source in sources
        ) else "failed"
        record = self._complete_request(request.request_id, status, sources)
        del image_bytes
        return record.as_payload(), self._should_recycle()

    def status(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            record = self._records.get(request_id)
            if record is None:
                return {
                    "request_id": request_id,
                    "status": "unknown",
                    "worker_instance_id": self.instance_id,
                    "worker_generation": self.generation,
                    "contract_version": self.config.contract_version,
                }
            return record.as_payload()

    def _validate_request(
        self,
        request: OcrRequest,
    ) -> tuple[bytes, str]:
        image_bytes = _decode_image(request.image_base64, self.config)
        _validate_image(image_bytes, request.mime_type, self.config)
        return image_bytes, _input_hash(request, image_bytes)

    def _validate_contract(self, request: OcrRequest) -> None:
        if request.contract_version != self.config.contract_version:
            raise WorkerRequestError(
                409,
                "verification_contract_mismatch",
                "request contract version does not match worker",
            )

    def _begin_request(
        self,
        request: OcrRequest,
        admission_hash: str,
    ) -> RequestRecord | None:
        with self._lock:
            self._cleanup_locked()
            existing = self._records.get(request.request_id)
            if existing is not None:
                if existing.admission_hash != admission_hash:
                    raise WorkerRequestError(
                        409,
                        "verification_contract_conflict",
                        "request_id was reused with different input",
                    )
                return existing
            if self._active_request_id:
                self._busy_rejections += 1
                raise WorkerRequestError(
                    409,
                    "verification_local_ocr_busy",
                    "image verification worker is busy",
                )
            record = RequestRecord(
                request_id=request.request_id,
                input_hash="",
                admission_hash=admission_hash,
                status="running",
                worker_instance_id=self.instance_id,
                worker_generation=self.generation,
                contract_version=self.config.contract_version,
                started_at=datetime.now(UTC).isoformat(),
            )
            self._records[request.request_id] = record
            self._active_request_id = request.request_id
            return None

    def _set_input_hash(self, request_id: str, input_hash: str) -> None:
        with self._lock:
            self._records[request_id].input_hash = input_hash

    def _submit_sources(
        self,
        image_bytes: bytes,
        deadline_monotonic: float,
    ) -> tuple[Future[SourceResult], Future[SourceResult]]:
        return (
            self._rapidocr.submit(
                _run_source,
                "rapidocr",
                image_verification_ocr.recognize_rapidocr_variants,
                image_bytes,
                deadline_monotonic=deadline_monotonic,
            ),
            self._ddddocr.submit(
                _run_source,
                "ddddocr",
                image_verification_ocr.recognize_ddddocr_variants,
                image_bytes,
                deadline_monotonic=deadline_monotonic,
            ),
        )

    def _complete_request(
        self,
        request_id: str,
        status: str,
        sources: tuple[SourceResult, ...],
        *,
        error_code: str = "",
    ) -> RequestRecord:
        with self._lock:
            record = self._records[request_id]
            record.status = status
            record.sources = sources
            record.completed_at = datetime.now(UTC).isoformat()
            record.error_code = error_code or _terminal_error_code(status)
            record.expires_at = datetime.now(UTC) + timedelta(
                seconds=self.config.terminal_ttl_seconds
            )
            self._active_request_id = ""
            self._completed_requests += 1
            return record

    def _cleanup_locked(self) -> None:
        now = datetime.now(UTC)
        expired = [
            request_id
            for request_id, record in self._records.items()
            if record.status != "running"
            and record.expires_at is not None
            and record.expires_at <= now
        ]
        for request_id in expired:
            self._records.pop(request_id, None)

    def _should_recycle(self) -> bool:
        with self._lock:
            if self._active_request_id:
                return False
            request_limit_reached = (
                self._completed_requests >= self.config.recycle_request_limit
            )
        return (
            request_limit_reached
            or self._rss_reader() >= self.config.soft_rss_bytes
        )

    def health(self) -> dict[str, Any]:
        with self._lock:
            active_request_id = self._active_request_id
            completed_requests = self._completed_requests
            busy_rejections = self._busy_rejections
        return {
            "status": "ok",
            "worker_instance_id": self.instance_id,
            "worker_generation": self.generation,
            "contract_version": self.config.contract_version,
            "active_request_id": active_request_id,
            "request_status": "running" if active_request_id else "idle",
            "completed_requests": completed_requests,
            "busy_rejections": busy_rejections,
            "rss_bytes": self._rss_reader(),
        }


def _run_source(
    source: str,
    recognize: Callable[[bytes], tuple[tuple[str, float], ...]],
    image_bytes: bytes,
    *,
    deadline_monotonic: float,
) -> SourceResult:
    started_at = datetime.now(UTC)
    started = monotonic()
    try:
        values = recognize(image_bytes)
        candidates = tuple(
            {"text": str(text), "confidence": float(confidence)}
            for text, confidence in values
        )
        status = "complete"
        detail = ""
    except Exception as exc:  # noqa: BLE001 - per-source failure is explicit.
        candidates = ()
        status = "failed"
        detail = exc.__class__.__name__
    completed = monotonic()
    return SourceResult(
        source=source,
        status=status,
        candidates=candidates,
        started_at=started_at.isoformat(),
        completed_at=datetime.now(UTC).isoformat(),
        duration_ms=max(0, int((completed - started) * 1000)),
        late=completed >= deadline_monotonic,
        detail=detail,
    )


def _terminal_error_code(status: str) -> str:
    return {
        "completed": "",
        "expired": "verification_deadline_exceeded",
    }.get(status, "verification_local_ocr_unavailable")


def _decode_image(encoded: str, config: WorkerConfig) -> bytes:
    max_encoded = int(config.max_image_bytes * BASE64_EXPANSION_RATIO) + 4
    if len(encoded) > max_encoded:
        raise WorkerRequestError(
            413,
            "verification_payload_too_large",
            "encoded image exceeds configured limit",
        )
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WorkerRequestError(
            422,
            "verification_payload_invalid",
            "image_base64 is invalid",
        ) from exc
    if not image_bytes or len(image_bytes) > config.max_image_bytes:
        raise WorkerRequestError(
            413,
            "verification_payload_too_large",
            "decoded image exceeds configured limit",
        )
    return image_bytes


def _validate_image(
    image_bytes: bytes,
    mime_type: str,
    config: WorkerConfig,
) -> None:
    if mime_type not in ALLOWED_MIME_TYPES:
        raise WorkerRequestError(
            422,
            "verification_payload_invalid",
            "unsupported image MIME type",
        )
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            width, height = image.size
            image.verify()
    except Exception as exc:  # noqa: BLE001 - decoder boundary.
        raise WorkerRequestError(
            422,
            "verification_payload_invalid",
            "image cannot be decoded",
        ) from exc
    if (
        width > config.max_dimension
        or height > config.max_dimension
        or width * height > config.max_image_pixels
    ):
        raise WorkerRequestError(
            413,
            "verification_payload_too_large",
            "image dimensions exceed configured limit",
        )


def _request_budget_seconds(
    request: OcrRequest,
    config: WorkerConfig,
) -> float:
    deadline = request.deadline_at.astimezone(UTC)
    wall_remaining = (deadline - datetime.now(UTC)).total_seconds()
    transmitted = request.remaining_budget_ms / 1000
    remaining = min(wall_remaining, transmitted, config.max_budget_seconds)
    if remaining <= 0:
        raise WorkerRequestError(
            410,
            "verification_deadline_exceeded",
            "request deadline has expired",
        )
    return remaining


def _input_hash(request: OcrRequest, image_bytes: bytes) -> str:
    value = "|".join(
        (
            request.action_id,
            request.challenge_fingerprint_hash,
            hashlib.sha256(image_bytes).hexdigest(),
            request.mime_type,
            request.verification_kind,
            request.candidate_hash,
            request.contract_version,
        )
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _admission_hash(request: OcrRequest) -> str:
    value = "|".join(
        (
            request.action_id,
            request.challenge_fingerprint_hash,
            hashlib.sha256(request.image_base64.encode()).hexdigest(),
            request.mime_type,
            request.verification_kind,
            request.candidate_hash,
            request.contract_version,
        )
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _schedule_abnormal_termination() -> None:
    threading.Timer(
        TERMINATION_DELAY_SECONDS,
        lambda: os.kill(os.getpid(), signal.SIGKILL),
    ).start()


def _schedule_graceful_recycle() -> None:
    threading.Timer(
        TERMINATION_DELAY_SECONDS,
        lambda: os.kill(os.getpid(), signal.SIGTERM),
    ).start()


def _authorize_service(
    service: ImageVerificationWorkerService,
    token: str | None,
) -> ImageVerificationWorkerService:
    if token is None or not hmac.compare_digest(token, service.config.token):
        raise HTTPException(status_code=401, detail={"code": "unauthorized"})
    return service


def create_app(
    service: ImageVerificationWorkerService | None = None,
    *,
    recycle_scheduler: Callable[[], None] = _schedule_graceful_recycle,
) -> FastAPI:
    app = FastAPI(title="TG Image Verification Worker")
    service_lock = threading.Lock()
    selected_service = service

    def current_service() -> ImageVerificationWorkerService:
        nonlocal selected_service
        with service_lock:
            if selected_service is None:
                selected_service = ImageVerificationWorkerService(
                    WorkerConfig.from_env()
                )
            return selected_service

    @app.get("/health")
    def health() -> dict[str, Any]:
        return current_service().health()

    @app.get("/internal/v1/image-verification/ready")
    def ready(x_internal_token: str | None = Header(default=None)) -> dict[str, Any]:
        _authorize_service(current_service(), x_internal_token)
        engines = image_verification_ocr.verify_engines_ready()
        return {"status": "ready", "engines": engines}

    @app.post("/internal/v1/image-verification/ocr")
    def execute(
        request: OcrRequest,
        x_internal_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        worker = _authorize_service(current_service(), x_internal_token)
        try:
            payload, recycle = worker.execute(request)
        except WorkerRequestError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "detail": str(exc)},
            ) from exc
        if recycle:
            recycle_scheduler()
        return payload

    @app.get("/internal/v1/image-verification/ocr/{request_id}")
    def status(
        request_id: str,
        x_internal_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return _authorize_service(
            current_service(), x_internal_token
        ).status(request_id)

    return app


app = create_app()
