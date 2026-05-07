from __future__ import annotations
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.routers import router as api_router
from .config import get_settings
from .database import SessionLocal, prepare_database
from .services import ensure_seed_data

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
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
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="TG 运营管理平台 API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    media_root = Path(settings.media_root)
    media_root.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(media_root)), name="media")
    app.include_router(api_router)
    return app


app = create_app()
