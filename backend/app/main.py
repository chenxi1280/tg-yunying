from __future__ import annotations
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
import logging
import threading

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.routers import router as api_router
from .config import get_settings
from .database import SessionLocal, prepare_database
from .permission_middleware import permission_middleware
from .services import ensure_seed_data
from .telethon_lifecycle import shutdown_telethon_lifecycle
from .worker import run_worker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    status = prepare_database()
    logger.info(
        "Database ready: tables=%s was_empty=%s alembic=%s previous_alembic=%s",
        status["table_count"],
        status["was_empty"],
        status["alembic_version"],
        status["previous_alembic_version"],
    )
    with SessionLocal() as session:
        ensure_seed_data(session)
    worker_stop_event: threading.Event | None = None
    worker_thread: threading.Thread | None = None
    if settings.enable_embedded_worker:
        worker_stop_event = threading.Event()
        worker_thread = threading.Thread(
            target=run_worker,
            kwargs={
                "limit": settings.embedded_worker_limit,
                "interval_seconds": settings.embedded_worker_interval_seconds,
                "stop_event": worker_stop_event,
                "role": settings.worker_role,
            },
            name="tg-yunying-embedded-worker",
            daemon=True,
        )
        worker_thread.start()
        logger.info(
            "Embedded worker started: role=%s interval=%ss limit=%s",
            settings.worker_role,
            settings.embedded_worker_interval_seconds,
            settings.embedded_worker_limit,
        )
    else:
        logger.info("Embedded worker disabled; run `python -m app.worker` as a separate process if needed.")
    try:
        yield
    finally:
        if worker_stop_event:
            worker_stop_event.set()
        if worker_thread:
            worker_thread.join(timeout=max(1.0, settings.embedded_worker_interval_seconds + 1.0))
            logger.info("Embedded worker stopped")
        try:
            disconnected = shutdown_telethon_lifecycle(timeout_seconds=5)
            if disconnected:
                logger.info("Telethon client lifecycle stopped: disconnected=%s", disconnected)
        except Exception:
            logger.exception("Telethon client lifecycle shutdown failed")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="TG 运营管理平台 API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Total-Count", "X-Page", "X-Page-Size"],
    )
    app.middleware("http")(permission_middleware)
    media_root = Path(settings.media_root)
    media_root.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(media_root)), name="media")
    app.include_router(api_router)
    return app


app = create_app()
