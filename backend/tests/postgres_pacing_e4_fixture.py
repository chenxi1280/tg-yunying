"""Isolated schemas for the window and E4 multi-connection regressions."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.database import Base, connect_args as application_connect_args


@pytest.fixture
def factory(postgres_test_session_lock):
    url = make_url(os.environ["TEST_DATABASE_URL"])
    assert url.database == "tg_yunying_test"
    schema = "pacing_e4_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={
            "options": f"{application_connect_args['options']} -csearch_path={schema} -clock_timeout=1000"})
        try:
            Base.metadata.create_all(engine)
            yield sessionmaker(bind=engine, autoflush=False)
        finally:
            engine.dispose()
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()
