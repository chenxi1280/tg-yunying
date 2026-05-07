from __future__ import annotations
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.routers import router as api_router
from .config import get_settings
from .database import Base, SessionLocal, engine, ensure_schema_compat, is_sqlite_url, run_migrations
from .services import ensure_seed_data


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if is_sqlite_url():
        Base.metadata.create_all(bind=engine)
        ensure_schema_compat()
    elif settings.auto_migrate_on_start:
        run_migrations()
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
