from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from time import monotonic
from typing import Callable, Generic, TypeVar

T = TypeVar("T")

SLOT_POLL_SECONDS = 0.01


class VerificationDeadlineExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageVerificationPolicy:
    enabled: bool
    contract_version: str
    callback_acceptance_seconds: float
    callback_headroom_seconds: float
    model_tail_budget_seconds: float
    model_timeout_seconds: float
    reasoning_retry_min_budget_seconds: float
    model_concurrency: int
    ocr_backend: str = "local"
    worker_url: str = ""
    worker_token: str = ""

    @classmethod
    def from_settings(cls, settings: object) -> "ImageVerificationPolicy":
        return cls(
            enabled=bool(settings.image_verification_contract_enabled),
            contract_version=str(settings.image_verification_contract_version),
            callback_acceptance_seconds=float(settings.image_verification_callback_acceptance_seconds),
            callback_headroom_seconds=float(settings.image_verification_callback_headroom_seconds),
            model_tail_budget_seconds=float(settings.image_verification_model_tail_budget_seconds),
            model_timeout_seconds=float(settings.image_verification_model_timeout_seconds),
            reasoning_retry_min_budget_seconds=float(settings.image_verification_reasoning_retry_min_budget_seconds),
            model_concurrency=int(settings.image_verification_model_concurrency),
            ocr_backend=str(settings.image_verification_ocr_backend),
            worker_url=str(settings.image_verification_worker_url),
            worker_token=str(settings.image_verification_worker_token),
        )

    def calibration_error(self) -> str:
        recognition_window = (
            self.callback_acceptance_seconds
            - self.callback_headroom_seconds
        )
        if not self.contract_version.strip():
            return "contract version is empty"
        if recognition_window <= 0:
            return "callback acceptance/headroom is not calibrated"
        if not 0 < self.model_tail_budget_seconds < recognition_window:
            return "model tail budget is not calibrated"
        if self.model_timeout_seconds <= 0:
            return "model timeout must be positive"
        if self.reasoning_retry_min_budget_seconds <= 0:
            return "reasoning retry minimum budget must be positive"
        if self.model_concurrency < 1:
            return "model concurrency must be positive"
        if self.ocr_backend not in {"local", "remote"}:
            return "OCR backend must be local or remote"
        if self.ocr_backend == "remote" and not (
            self.worker_url.strip() and self.worker_token.strip()
        ):
            return "remote OCR worker URL/token is missing"
        return ""


@dataclass(frozen=True)
class VerificationBudget:
    observed_at: datetime
    observed_monotonic: float
    callback_deadline_at: datetime
    callback_deadline_monotonic: float
    model_hedge_at: datetime
    model_hedge_monotonic: float

    @classmethod
    def create(
        cls,
        policy: ImageVerificationPolicy,
        *,
        observed_at: datetime | None = None,
        observed_monotonic: float | None = None,
    ) -> "VerificationBudget":
        wall_started = observed_at or datetime.now(UTC)
        mono_started = (
            monotonic() if observed_monotonic is None else observed_monotonic
        )
        window = (
            policy.callback_acceptance_seconds
            - policy.callback_headroom_seconds
        )
        callback_at = wall_started + timedelta(seconds=window)
        callback_mono = mono_started + window
        hedge_at = callback_at - timedelta(
            seconds=policy.model_tail_budget_seconds
        )
        return cls(
            observed_at=wall_started,
            observed_monotonic=mono_started,
            callback_deadline_at=callback_at,
            callback_deadline_monotonic=callback_mono,
            model_hedge_at=hedge_at,
            model_hedge_monotonic=(
                callback_mono - policy.model_tail_budget_seconds
            ),
        )

    def remaining_seconds(self) -> float:
        return max(0.0, self.callback_deadline_monotonic - monotonic())

    def seconds_until_hedge(self) -> float:
        return max(0.0, self.model_hedge_monotonic - monotonic())

    def ensure_remaining(self) -> None:
        if self.remaining_seconds() <= 0:
            raise VerificationDeadlineExceeded(
                "image verification deadline exceeded"
            )


@dataclass(frozen=True)
class TimedResult(Generic[T]):
    value: T | None
    started_at: datetime
    completed_at: datetime
    duration_ms: int
    completed_monotonic: float
    error_type: str = ""


class ActiveOperationRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._futures: set[Future[object]] = set()

    def track(self, future: Future[T]) -> Future[T]:
        with self._lock:
            self._futures.add(future)
        future.add_done_callback(self._discard)
        return future

    def _discard(self, future: Future[object]) -> None:
        with self._lock:
            self._futures.discard(future)

    def count(self) -> int:
        with self._lock:
            return len(self._futures)

    def wait_empty(self, stop_event: threading.Event | None = None) -> bool:
        while self.count():
            if stop_event and stop_event.wait(SLOT_POLL_SECONDS):
                return False
            if stop_event is None:
                threading.Event().wait(SLOT_POLL_SECONDS)
        return True


class FixedOperationSlot:
    def __init__(self, name: str, registry: ActiveOperationRegistry) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"image-verification-{name}",
        )
        self._semaphore = threading.BoundedSemaphore(1)
        self._registry = registry

    def try_submit(
        self,
        operation: Callable[[], T],
    ) -> Future[TimedResult[T]] | None:
        if not self._semaphore.acquire(blocking=False):
            return None
        future = self._executor.submit(self._run, operation)
        return self._registry.track(future)

    def _run(self, operation: Callable[[], T]) -> TimedResult[T]:
        started_at = datetime.now(UTC)
        started = monotonic()
        try:
            value = operation()
            error_type = ""
        except Exception as exc:  # noqa: BLE001 - source failure is a vote fact.
            value = None
            error_type = exc.__class__.__name__
        finally:
            self._semaphore.release()
        completed_at = datetime.now(UTC)
        completed_monotonic = monotonic()
        return TimedResult(
            value=value,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=max(0, int((completed_monotonic - started) * 1000)),
            completed_monotonic=completed_monotonic,
            error_type=error_type,
        )


class ImageVerificationRuntime:
    def __init__(self, model_concurrency: int) -> None:
        self.registry = ActiveOperationRegistry()
        self.rapidocr = FixedOperationSlot("rapidocr", self.registry)
        self.ddddocr = FixedOperationSlot("ddddocr", self.registry)
        self._model_executor = ThreadPoolExecutor(
            max_workers=model_concurrency,
            thread_name_prefix="image-verification-model",
        )
        self._remote_executor = ThreadPoolExecutor(
            max_workers=model_concurrency,
            thread_name_prefix="image-verification-remote-ocr",
        )
        self._challenge_lock = threading.Lock()
        self._challenge_count = 0

    def submit_model(self, operation: Callable[[], T]) -> Future[T]:
        return self.registry.track(self._model_executor.submit(operation))

    def submit_remote_ocr(self, operation: Callable[[], T]) -> Future[T]:
        return self.registry.track(self._remote_executor.submit(operation))

    def record_challenge(self) -> int:
        with self._challenge_lock:
            self._challenge_count += 1
            return self._challenge_count

    def challenge_count(self) -> int:
        with self._challenge_lock:
            return self._challenge_count


@lru_cache(maxsize=8)
def get_image_verification_runtime(
    model_concurrency: int,
) -> ImageVerificationRuntime:
    return ImageVerificationRuntime(model_concurrency)
