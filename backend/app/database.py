from __future__ import annotations
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[1]
DATABASE_URL = get_settings().database_url
settings = get_settings()


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {"options": "-c timezone=Asia/Shanghai"}
engine_kwargs = {"connect_args": connect_args, "future": True}
if not DATABASE_URL.startswith("sqlite"):
    engine_kwargs.update(
        {
            "pool_size": max(1, int(settings.db_pool_size or 5)),
            "max_overflow": max(0, int(settings.db_max_overflow or 0)),
            "pool_timeout": max(1, int(settings.db_pool_timeout or 30)),
            "pool_recycle": max(60, int(settings.db_pool_recycle or 1800)),
        }
    )
engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _escape_configparser_value(value: str) -> str:
    return value.replace("%", "%%")


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def run_migrations() -> None:
    """Apply Alembic migrations for the configured database."""
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", _escape_configparser_value(DATABASE_URL))
    command.upgrade(alembic_cfg, "head")


def database_status() -> dict[str, object]:
    """Return a small startup-facing snapshot of the configured database."""
    with engine.connect() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        version = None
        if "alembic_version" in tables:
            version = connection.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).scalar()
        timezone = "Asia/Shanghai" if DATABASE_URL.startswith("sqlite") else connection.execute(text("SHOW timezone")).scalar()
        return {
            "url": DATABASE_URL,
            "is_empty": not tables,
            "table_count": len(tables),
            "alembic_version": version,
            "timezone": timezone,
        }


def prepare_database() -> dict[str, object]:
    """Connect, initialize empty databases, and migrate older schemas before app startup."""
    before = database_status()
    run_migrations()
    after = database_status()
    return {
        **after,
        "was_empty": before["is_empty"],
        "previous_alembic_version": before["alembic_version"],
    }
