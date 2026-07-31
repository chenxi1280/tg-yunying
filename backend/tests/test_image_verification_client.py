from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.integrations.telegram.search_join import ImageVerificationRequest
from app.services.image_verification_client import (
    ImageVerificationWorkerClient,
    RemoteOcrError,
    deterministic_request_id,
)
from app.services.image_verification_runtime import (
    ImageVerificationPolicy,
    VerificationBudget,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def _policy() -> ImageVerificationPolicy:
    return ImageVerificationPolicy(
        enabled=True,
        contract_version="test-v1",
        callback_acceptance_seconds=5,
        callback_headroom_seconds=1,
        model_tail_budget_seconds=2,
        model_timeout_seconds=2,
        reasoning_retry_min_budget_seconds=0.5,
        model_concurrency=1,
        ocr_backend="remote",
        worker_url="http://image-verification-worker:8091",
        worker_token="test-token",
    )


def _request() -> ImageVerificationRequest:
    return ImageVerificationRequest(
        image_bytes=b"image",
        mime_type="image/png",
        candidate_answers=("9", "10"),
        challenge_text="数学题",
        challenge_fingerprint_hash="b" * 64,
        candidate_hash="c" * 64,
        challenge_observed_at=datetime.now(UTC),
    )


def _expected_request_id() -> str:
    return deterministic_request_id("action-1", "b" * 64, "test-v1")


def _completed_payload() -> dict:
    return {
        "request_id": _expected_request_id(),
        "input_hash": "d" * 64,
        "status": "completed",
        "worker_generation": "generation-1",
        "contract_version": "test-v1",
        "sources": [
            {
                "source": source,
                "status": "complete",
                "candidates": [{"text": "10", "confidence": 0.9}],
                "started_at": "start",
                "completed_at": "end",
                "duration_ms": 10,
                "late": False,
                "detail": "",
            }
            for source in ("rapidocr", "ddddocr")
        ],
    }


@pytest.mark.no_postgres
def test_request_id_is_deterministic_and_contract_scoped() -> None:
    first = deterministic_request_id("action", "fingerprint", "v1")

    assert first == deterministic_request_id("action", "fingerprint", "v1")
    assert first != deterministic_request_id("action", "fingerprint", "v2")


@pytest.mark.no_postgres
def test_post_timeout_queries_status_without_duplicate_post(monkeypatch) -> None:
    methods: list[str] = []

    def fake_urlopen(request, timeout):
        del timeout
        methods.append(request.method)
        if request.method == "POST":
            raise TimeoutError
        return _Response(
            {
                    "request_id": _expected_request_id(),
                    "status": "running",
                    "worker_generation": "generation-1",
                    "contract_version": "test-v1",
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    policy = _policy()
    budget = VerificationBudget.create(
        policy,
        observed_at=datetime.now(UTC),
    )

    with pytest.raises(RemoteOcrError) as raised:
        ImageVerificationWorkerClient(policy).recognize(
            "action-1",
            _request(),
            budget,
        )

    assert raised.value.code == "verification_local_ocr_timeout"
    assert methods == ["POST", "GET"]


@pytest.mark.no_postgres
def test_status_query_network_failure_forbids_duplicate_post(
    monkeypatch,
) -> None:
    methods: list[str] = []

    def fake_urlopen(request, timeout):
        del timeout
        methods.append(request.method)
        if request.method == "POST":
            raise TimeoutError
        raise urllib.error.URLError("network down")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    policy = _policy()

    with pytest.raises(RemoteOcrError) as raised:
        ImageVerificationWorkerClient(policy).recognize(
            "action-1",
            _request(),
            VerificationBudget.create(policy),
        )

    assert raised.value.code == "verification_local_ocr_timeout"
    assert methods == ["POST", "GET"]


@pytest.mark.no_postgres
def test_running_post_response_is_explicit_timeout(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda _request, timeout: _Response(
            {
                    "request_id": _expected_request_id(),
                    "status": "running",
                    "worker_generation": "generation-1",
                    "contract_version": "test-v1",
            }
        ),
    )
    policy = _policy()

    with pytest.raises(RemoteOcrError) as raised:
        ImageVerificationWorkerClient(policy).recognize(
            "action-1",
            _request(),
            VerificationBudget.create(policy),
        )

    assert raised.value.code == "verification_local_ocr_timeout"


@pytest.mark.no_postgres
def test_completed_remote_response_preserves_two_source_results(
    monkeypatch,
) -> None:
    payload = _completed_payload()
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda _request, timeout: (
            _Response(payload) if timeout > 0 else SimpleNamespace()
        ),
    )
    policy = _policy()

    result = ImageVerificationWorkerClient(policy).recognize(
        "action-1",
        _request(),
        VerificationBudget.create(policy),
    )

    assert [source.source for source in result.sources] == [
        "rapidocr",
        "ddddocr",
    ]


@pytest.mark.no_postgres
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("request_id", "f" * 64),
        ("contract_version", "wrong-v2"),
    ),
)
def test_completed_response_rejects_wrong_identity(
    monkeypatch,
    field,
    value,
) -> None:
    payload = {**_completed_payload(), field: value}
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda _request, timeout: _Response(payload),
    )

    with pytest.raises(RemoteOcrError) as raised:
        ImageVerificationWorkerClient(_policy()).recognize(
            "action-1",
            _request(),
            VerificationBudget.create(_policy()),
        )

    assert raised.value.code == "verification_contract_mismatch"


@pytest.mark.no_postgres
def test_completed_response_requires_both_unique_sources(monkeypatch) -> None:
    payload = {**_completed_payload(), "sources": []}
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda _request, timeout: _Response(payload),
    )

    with pytest.raises(RemoteOcrError) as raised:
        ImageVerificationWorkerClient(_policy()).recognize(
            "action-1",
            _request(),
            VerificationBudget.create(_policy()),
        )

    assert raised.value.code == "verification_contract_mismatch"


@pytest.mark.no_postgres
def test_completed_response_maps_malformed_source_to_contract_error(
    monkeypatch,
) -> None:
    payload = _completed_payload()
    payload["sources"][0]["candidates"][0]["confidence"] = "not-a-number"
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda _request, timeout: _Response(payload),
    )

    with pytest.raises(RemoteOcrError) as raised:
        ImageVerificationWorkerClient(_policy()).recognize(
            "action-1",
            _request(),
            VerificationBudget.create(_policy()),
        )

    assert raised.value.code == "verification_contract_mismatch"


@pytest.mark.no_postgres
def test_http_timeout_error_queries_status_without_duplicate_post(
    monkeypatch,
) -> None:
    client = ImageVerificationWorkerClient(_policy())
    methods: list[str] = []

    def request_json(method, _path, _payload, _timeout):
        methods.append(method)
        if method == "POST":
            raise RemoteOcrError(
                "verification_local_ocr_timeout",
                "worker returned HTTP 504",
            )
        return _completed_payload()

    monkeypatch.setattr(client, "_request_json", request_json)

    result = client.recognize(
        "action-1",
        _request(),
        VerificationBudget.create(_policy()),
    )

    assert result.request_id == _expected_request_id()
    assert methods == ["POST", "GET"]
