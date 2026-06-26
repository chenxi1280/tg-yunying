from __future__ import annotations

import ast
from pathlib import Path

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
