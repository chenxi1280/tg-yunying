from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO
from threading import Event, Lock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.image_verification_worker import (
    ImageVerificationWorkerService,
    OcrRequest,
    WorkerRequestError,
    _run_source,
    create_app,
)
from app import image_verification_worker
from app.image_verification_worker_config import WorkerConfig
from app import image_verification_ocr


def _config() -> WorkerConfig:
    return WorkerConfig(
        token="test-token",
        contract_version="test-v1",
        max_image_bytes=100_000,
        max_image_pixels=100_000,
        max_dimension=1_000,
        max_budget_seconds=5,
        recovery_observation_seconds=5,
        terminal_ttl_seconds=30,
        recycle_request_limit=100,
        soft_rss_bytes=1_000_000,
    )


def _image_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (10, 10), "white").save(output, format="PNG")
    return output.getvalue()


def _request(
    request_id: str = "a" * 64,
    *,
    candidate_hash: str = "c" * 64,
) -> OcrRequest:
    return OcrRequest(
        request_id=request_id,
        action_id="action-1",
        challenge_fingerprint_hash="b" * 64,
        image_base64=base64.b64encode(_image_bytes()).decode(),
        mime_type="image/png",
        verification_kind="math",
        candidate_hash=candidate_hash,
        deadline_at=datetime.now(UTC) + timedelta(seconds=5),
        remaining_budget_ms=5_000,
        contract_version="test-v1",
    )


def _install_ocr(monkeypatch) -> None:
    monkeypatch.setattr(
        image_verification_ocr,
        "recognize_rapidocr_variants",
        lambda _image: (("10", 0.9),),
    )
    monkeypatch.setattr(
        image_verification_ocr,
        "recognize_ddddocr_variants",
        lambda _image: (("10", 0.8),),
    )


@pytest.mark.no_postgres
def test_worker_requires_private_token_and_returns_no_image(
    monkeypatch,
) -> None:
    _install_ocr(monkeypatch)
    recycle_calls: list[str] = []
    service = ImageVerificationWorkerService(
        _config(),
        abnormal_terminate=lambda: None,
        rss_reader=lambda: 0,
    )
    client = TestClient(
        create_app(
            service,
            recycle_scheduler=lambda: recycle_calls.append("scheduled"),
        )
    )

    unauthorized = client.post(
        "/internal/v1/image-verification/ocr",
        json=_request().model_dump(mode="json"),
    )
    response = client.post(
        "/internal/v1/image-verification/ocr",
        json=_request().model_dump(mode="json"),
        headers={"X-Internal-Token": "test-token"},
    )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert "image" not in str(response.json()).lower()
    health = client.get("/health").json()
    assert health["request_status"] == "idle"
    assert health["completed_requests"] == 1
    assert health["busy_rejections"] == 0
    assert recycle_calls == []


@pytest.mark.no_postgres
def test_worker_functional_readiness_requires_token_and_initializes_engines(
    monkeypatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        image_verification_ocr,
        "verify_engines_ready",
        lambda: calls.append("ready") or ("rapidocr", "ddddocr"),
    )
    client = TestClient(
        create_app(
            ImageVerificationWorkerService(
                _config(),
                abnormal_terminate=lambda: None,
            )
        )
    )

    unauthorized = client.get("/internal/v1/image-verification/ready")
    ready = client.get(
        "/internal/v1/image-verification/ready",
        headers={"X-Internal-Token": "test-token"},
    )

    assert unauthorized.status_code == 401
    assert ready.json() == {
        "status": "ready",
        "engines": ["rapidocr", "ddddocr"],
    }
    assert calls == ["ready"]


@pytest.mark.no_postgres
def test_worker_reuses_same_request_and_rejects_contract_conflict(
    monkeypatch,
) -> None:
    _install_ocr(monkeypatch)
    service = ImageVerificationWorkerService(
        _config(),
        abnormal_terminate=lambda: None,
    )

    first, _ = service.execute(_request())
    repeated, _ = service.execute(_request())

    assert repeated == first
    with pytest.raises(WorkerRequestError) as raised:
        service.execute(_request(candidate_hash="d" * 64))
    assert raised.value.code == "verification_contract_conflict"


@pytest.mark.no_postgres
def test_worker_rejects_second_request_without_queueing(monkeypatch) -> None:
    release = Event()
    entered = Event()

    def slow_ocr(_image):
        entered.set()
        release.wait(timeout=2)
        return (("10", 0.9),)

    monkeypatch.setattr(
        image_verification_ocr,
        "recognize_rapidocr_variants",
        slow_ocr,
    )
    monkeypatch.setattr(
        image_verification_ocr,
        "recognize_ddddocr_variants",
        slow_ocr,
    )
    service = ImageVerificationWorkerService(
        _config(),
        abnormal_terminate=lambda: None,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(service.execute, _request())
        assert entered.wait(timeout=1)
        with pytest.raises(WorkerRequestError) as raised:
            service.execute(
                _request("e" * 64).model_copy(
                    update={"image_base64": "not-valid-base64"}
                )
            )
        release.set()
        running.result(timeout=2)

    assert raised.value.code == "verification_local_ocr_busy"


@pytest.mark.no_postgres
def test_worker_admission_precedes_base64_decode(monkeypatch) -> None:
    entered = Event()
    release = Event()
    counter_lock = Lock()
    decode_calls = 0
    original_decode = image_verification_worker._decode_image

    def slow_decode(encoded, config):
        nonlocal decode_calls
        with counter_lock:
            decode_calls += 1
        entered.set()
        release.wait(timeout=2)
        return original_decode(encoded, config)

    monkeypatch.setattr(image_verification_worker, "_decode_image", slow_decode)
    _install_ocr(monkeypatch)
    service = ImageVerificationWorkerService(_config(), abnormal_terminate=lambda: None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.execute, _request())
        assert entered.wait(timeout=1)
        second = executor.submit(service.execute, _request("e" * 64))
        try:
            with pytest.raises(WorkerRequestError) as raised:
                second.result(timeout=1)
        finally:
            release.set()
            first.result(timeout=2)

    assert raised.value.code == "verification_local_ocr_busy"
    assert decode_calls == 1


@pytest.mark.no_postgres
def test_worker_missing_status_is_generation_scoped_unknown() -> None:
    service = ImageVerificationWorkerService(
        _config(),
        abnormal_terminate=lambda: None,
    )

    status = service.status("f" * 64)

    assert status["status"] == "unknown"
    assert status["worker_generation"] == service.generation


@pytest.mark.no_postgres
def test_worker_recycles_after_terminal_request_at_soft_rss(
    monkeypatch,
) -> None:
    _install_ocr(monkeypatch)
    service = ImageVerificationWorkerService(
        _config(),
        abnormal_terminate=lambda: None,
        rss_reader=lambda: _config().soft_rss_bytes,
    )

    payload, recycle = service.execute(_request())

    assert payload["status"] == "completed"
    assert recycle is True


@pytest.mark.no_postgres
def test_worker_endpoint_schedules_recycle_without_signaling_process(
    monkeypatch,
) -> None:
    _install_ocr(monkeypatch)
    recycle_calls: list[str] = []
    service = ImageVerificationWorkerService(
        _config(),
        abnormal_terminate=lambda: None,
        rss_reader=lambda: _config().soft_rss_bytes,
    )
    client = TestClient(
        create_app(
            service,
            recycle_scheduler=lambda: recycle_calls.append("scheduled"),
        )
    )

    response = client.post(
        "/internal/v1/image-verification/ocr",
        json=_request().model_dump(mode="json"),
        headers={"X-Internal-Token": "test-token"},
    )

    assert response.status_code == 200
    assert recycle_calls == ["scheduled"]


@pytest.mark.no_postgres
def test_worker_rejects_expired_deadline_before_ocr(monkeypatch) -> None:
    def should_not_run(_image):
        raise AssertionError("expired request must not run OCR")

    monkeypatch.setattr(
        image_verification_ocr,
        "recognize_rapidocr_variants",
        should_not_run,
    )
    request = _request().model_copy(
        update={"deadline_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    service = ImageVerificationWorkerService(
        _config(),
        abnormal_terminate=lambda: None,
    )

    with pytest.raises(WorkerRequestError) as raised:
        service.execute(request)

    assert raised.value.code == "verification_deadline_exceeded"
    status = service.status(request.request_id)
    assert status["status"] == "expired"
    assert status["error_code"] == "verification_deadline_exceeded"


@pytest.mark.no_postgres
def test_worker_rejects_expired_deadline_before_decode(monkeypatch) -> None:
    monkeypatch.setattr(
        image_verification_worker,
        "_decode_image",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("expired request must not decode image")
        ),
    )
    request = _request().model_copy(
        update={"deadline_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    service = ImageVerificationWorkerService(_config(), abnormal_terminate=lambda: None)

    with pytest.raises(WorkerRequestError) as raised:
        service.execute(request)

    assert raised.value.code == "verification_deadline_exceeded"


@pytest.mark.no_postgres
def test_worker_source_marks_result_completed_after_deadline_as_late() -> None:
    result = _run_source(
        "rapidocr",
        lambda _image: (("10", 0.9),),
        b"image",
        deadline_monotonic=0,
    )

    assert result.status == "complete"
    assert result.late is True
