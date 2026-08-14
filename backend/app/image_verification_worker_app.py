from __future__ import annotations

import hmac
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException

from app import image_verification_ocr
from app.image_verification_worker import (
    ImageVerificationWorkerService,
    _schedule_graceful_recycle,
)
from app.image_verification_worker_config import WorkerConfig
from app.image_verification_worker_contract import OcrRequest, WorkerRequestError


class ImageVerificationWorkerApi:
    def __init__(
        self,
        service: ImageVerificationWorkerService | None,
        recycle_scheduler: Callable[[], None],
    ) -> None:
        self._service = service
        self._service_lock = threading.Lock()
        self._recycle_scheduler = recycle_scheduler

    def health(self) -> dict[str, Any]:
        return self._current_service().health()

    def ready(
        self,
        x_internal_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        worker = self._authorize(x_internal_token)
        if worker.health()["draining"]:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "verification_local_ocr_draining",
                    "detail": "image verification worker generation is draining",
                },
            )
        engines = image_verification_ocr.verify_engines_ready()
        return {"status": "ready", "engines": engines}

    def execute(
        self,
        request: OcrRequest,
        x_internal_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        try:
            payload, recycle = self._authorize(x_internal_token).execute(request)
        except WorkerRequestError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "detail": str(exc)},
            ) from exc
        if recycle:
            self._recycle_scheduler()
        return payload

    def status(
        self,
        request_id: str,
        x_internal_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return self._authorize(x_internal_token).status(request_id)

    def _authorize(self, token: str | None) -> ImageVerificationWorkerService:
        service = self._current_service()
        if token is None or not hmac.compare_digest(token, service.config.token):
            raise HTTPException(status_code=401, detail={"code": "unauthorized"})
        return service

    def _current_service(self) -> ImageVerificationWorkerService:
        with self._service_lock:
            if self._service is None:
                self._service = ImageVerificationWorkerService(
                    WorkerConfig.from_env()
                )
            return self._service


@asynccontextmanager
async def _worker_lifespan(_: FastAPI) -> AsyncIterator[None]:
    image_verification_ocr.verify_engines_ready()
    yield


def create_app(
    service: ImageVerificationWorkerService | None = None,
    *,
    recycle_scheduler: Callable[[], None] = _schedule_graceful_recycle,
    warmup_engines: bool = False,
) -> FastAPI:
    worker_api = ImageVerificationWorkerApi(service, recycle_scheduler)
    app = FastAPI(
        title="TG Image Verification Worker",
        lifespan=_worker_lifespan if warmup_engines else None,
    )
    app.add_api_route("/health", worker_api.health, methods=["GET"])
    app.add_api_route(
        "/internal/v1/image-verification/ready",
        worker_api.ready,
        methods=["GET"],
    )
    app.add_api_route(
        "/internal/v1/image-verification/ocr",
        worker_api.execute,
        methods=["POST"],
    )
    app.add_api_route(
        "/internal/v1/image-verification/ocr/{request_id}",
        worker_api.status,
        methods=["GET"],
    )
    return app


app = create_app(warmup_engines=True)
