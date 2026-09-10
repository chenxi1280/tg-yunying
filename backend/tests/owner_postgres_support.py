"""Isolated PostgreSQL schemas for authorization-owner regression evidence."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from app.database import Base


@pytest.fixture
def owner_engine(postgres_test_session_lock):
    url = make_url(os.environ['TEST_DATABASE_URL'])
    assert url.database == 'tg_yunying_test'
    admin = create_engine(url)
    schema = 'authorization_owner_' + uuid4().hex
    with admin.connect().execution_options(isolation_level='AUTOCOMMIT') as control:
        assert control.scalar(text('select current_database()')) == 'tg_yunying_test'
        control.execute(CreateSchema(schema))
        scoped = create_engine(url, connect_args={'options': '-csearch_path=' + schema})
        try:
            Base.metadata.create_all(scoped)
            yield scoped
        finally:
            scoped.dispose()
            control.execute(DropSchema(schema, cascade=True))
    admin.dispose()
