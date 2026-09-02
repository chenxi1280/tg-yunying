from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest


pytestmark = pytest.mark.no_postgres

CONFTST_PATH = Path(__file__).with_name("conftest.py")


def _module_level_reset_calls(source: str) -> list[int]:
    module = ast.parse(source)
    reset_call_lines: list[int] = []
    for node in module.body:
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
            if call.func.id == "_reset_test_database":
                reset_call_lines.append(node.lineno)
    return reset_call_lines


def test_conftest_does_not_reset_postgres_during_module_import():
    source = CONFTST_PATH.read_text(encoding="utf-8")

    assert _module_level_reset_calls(source) == []


def test_conftest_splits_source_only_tests_from_postgres_integration_tests():
    source = CONFTST_PATH.read_text(encoding="utf-8")

    assert "@pytest.hookimpl(trylast=True)" in source
    assert "pytest_collection_modifyitems" in source
    assert "no_postgres" in source
    assert "_selected_tests_require_postgres" in source


def test_postgres_reset_failure_is_reported_as_actionable_pytest_error():
    source = CONFTST_PATH.read_text(encoding="utf-8")

    assert "SQLAlchemyError" in source
    assert "except (RuntimeError, SQLAlchemyError) as exc:" in source
    assert "pytest.UsageError" in source
    assert "PostgreSQL test database is required" in source
    assert "Root cause: {type(exc).__name__}: {exc}" in source


def test_conftest_validates_test_database_url_safety():
    from conftest import _validate_test_database_url

    _validate_test_database_url("postgresql+psycopg://tester:tester@127.0.0.1:5432/tg_yunying_test")
    for forbidden_url in [
        "postgresql+psycopg://xixi:pwd@10.0.0.1:5432/tgyunying",
        "postgresql+psycopg://xixi_dev:pwd@10.0.0.1:5432/xixi_dev",
        "postgresql+psycopg://user:pwd@10.0.0.1:5432/latest_prod_test",
        "postgresql+psycopg://user:pwd@10.0.0.1:5432/test_db",
    ]:
        with pytest.raises(RuntimeError, match="Refusing to run integration tests against non-test database"):
            _validate_test_database_url(forbidden_url)


@pytest.mark.parametrize("app_env", ["prod", "production", "PRODUCTION"])
def test_conftest_blocks_integration_tests_in_production_environment(monkeypatch, app_env):
    from conftest import _validate_test_database_url

    monkeypatch.setenv("APP_ENV", app_env)
    with pytest.raises(RuntimeError, match="Cannot run database integration tests in production environment"):
        _validate_test_database_url("postgresql+psycopg://tester:tester@127.0.0.1:5432/tg_yunying_test")


def test_conftest_requires_explicit_test_database_url(monkeypatch):
    from conftest import _postgres_test_database_url

    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pwd@host:5432/tg_yunying_test")
    with pytest.raises(RuntimeError, match="TEST_DATABASE_URL must explicitly point"):
        _postgres_test_database_url()


def test_conftest_invalid_url_error_hides_credentials():
    from conftest import _validate_test_database_url

    with pytest.raises(RuntimeError) as exc_info:
        _validate_test_database_url("not-postgres://secret-user:secret-password@host/tg_yunying_test")
    assert "secret-user" not in str(exc_info.value)
    assert "secret-password" not in str(exc_info.value)


def test_conftest_uses_session_advisory_lock(monkeypatch):
    import conftest

    connection = MagicMock()
    connection.execution_options.return_value = connection
    connection.scalar.side_effect = ["tg_yunying_test", True, True]
    engine = MagicMock()
    engine.connect.return_value = connection
    monkeypatch.setattr(conftest, "_create_test_database_engine", lambda _url: engine)

    conftest._acquire_test_database_session_lock(
        "postgresql+psycopg://tester:tester@127.0.0.1:5432/tg_yunying_test"
    )
    conftest._release_test_database_session_lock()

    sql_calls = [str(call.args[0]) for call in connection.scalar.call_args_list]
    assert sql_calls == [
        "SELECT current_database()",
        "SELECT pg_try_advisory_lock(:lock_key)",
        "SELECT pg_advisory_unlock(:lock_key)",
    ]
    connection.close.assert_called_once()
    engine.dispose.assert_called_once()


def test_conftest_rejects_concurrent_session(monkeypatch):
    import conftest

    connection = MagicMock()
    connection.execution_options.return_value = connection
    connection.scalar.side_effect = ["tg_yunying_test", False]
    engine = MagicMock()
    engine.connect.return_value = connection
    monkeypatch.setattr(conftest, "_create_test_database_engine", lambda _url: engine)

    with pytest.raises(RuntimeError, match="Another pytest session is already using"):
        conftest._acquire_test_database_session_lock(
            "postgresql+psycopg://tester:tester@127.0.0.1:5432/tg_yunying_test"
        )

    connection.close.assert_called_once()
    engine.dispose.assert_called_once()


def test_conftest_reset_database_is_transactional(monkeypatch):
    import conftest

    connection = MagicMock()
    connection.scalar.return_value = "tg_yunying_test"
    transaction = MagicMock()
    transaction.__enter__.return_value = connection
    engine = MagicMock()
    engine.begin.return_value = transaction
    monkeypatch.setattr(conftest, "_create_test_database_engine", lambda _url: engine)
    monkeypatch.setattr(conftest, "_TEST_DATABASE_LOCK_CONNECTION", MagicMock())

    conftest._reset_test_database(
        "postgresql+psycopg://tester:tester@127.0.0.1:5432/tg_yunying_test"
    )

    engine.begin.assert_called_once_with()
    executed_sql = [str(call.args[0]) for call in connection.execute.call_args_list]
    assert executed_sql == [
        "DROP SCHEMA IF EXISTS public CASCADE",
        "CREATE SCHEMA IF NOT EXISTS public",
    ]
