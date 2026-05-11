from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.exc import OperationalError
from sqlalchemy import create_engine, text


os.environ.setdefault("APP_ENV", "test")
BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

for env_path in (PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"):
    if env_path.exists():
        load_dotenv(env_path, override=False)

test_database_url = os.getenv("TEST_DATABASE_URL")
if not test_database_url:
    test_database_url = f"sqlite:///{Path(tempfile.gettempdir()) / 'tg_yunying_test.sqlite3'}"
if test_database_url.startswith("postgresql+asyncpg://"):
    test_database_url = test_database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
elif test_database_url.startswith("postgresql://"):
    test_database_url = test_database_url.replace("postgresql://", "postgresql+psycopg://", 1)
elif test_database_url.startswith("postgres://"):
    test_database_url = test_database_url.replace("postgres://", "postgresql+psycopg://", 1)
os.environ["DATABASE_URL"] = test_database_url
os.environ["TEST_DATABASE_URL"] = test_database_url
os.environ["ADMIN_BOOTSTRAP_USERNAME"] = "admin@demo.local"
os.environ["ADMIN_BOOTSTRAP_EMAIL"] = "admin@demo.local"
os.environ["ADMIN_BOOTSTRAP_PASSWORD"] = "admin123"
os.environ["TG_API_ID"] = ""
os.environ["TG_API_HASH"] = ""
os.environ.setdefault("AUTO_MIGRATE_ON_START", "true")
os.environ["ENABLE_EMBEDDED_WORKER"] = "false"


def _reset_test_database() -> None:
    global test_database_url
    if test_database_url.startswith("sqlite"):
        db_path = Path(test_database_url.removeprefix("sqlite:///"))
        db_path.unlink(missing_ok=True)
        return
    try:
        engine = create_engine(test_database_url, future=True, isolation_level="AUTOCOMMIT")
        with engine.connect() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        engine.dispose()
    except OperationalError:
        test_database_url = f"sqlite:///{Path(tempfile.gettempdir()) / 'tg_yunying_test.sqlite3'}"
        os.environ["DATABASE_URL"] = test_database_url
        os.environ["TEST_DATABASE_URL"] = test_database_url
        Path(test_database_url.removeprefix("sqlite:///")).unlink(missing_ok=True)


_reset_test_database()
