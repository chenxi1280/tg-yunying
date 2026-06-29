from configparser import ConfigParser
import ast
from pathlib import Path
import subprocess
import sys

import pytest

from app.database import _escape_configparser_value


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_escape_configparser_value_allows_url_encoded_password() -> None:
    parser = ConfigParser()
    parser.add_section("alembic")
    database_url = "postgresql+psycopg://app_user:secret%2Bvalue@postgres:5432/tgyunying"

    parser.set("alembic", "sqlalchemy.url", _escape_configparser_value(database_url))

    assert parser.get("alembic", "sqlalchemy.url") == database_url


def test_alembic_revision_ids_fit_version_table() -> None:
    version_dir = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    too_long: list[str] = []

    for migration in version_dir.glob("*.py"):
        tree = ast.parse(migration.read_text(), filename=str(migration))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "revision" for target in node.targets):
                continue
            revision = ast.literal_eval(node.value)
            if len(revision) > 32:
                too_long.append(f"{migration.name}:{revision}")

    assert not too_long


def test_repair_admin_tables_downgrade_preserves_existing_auth_tables() -> None:
    migration = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0046_repair_admin_tables.py"
    tree = ast.parse(migration.read_text(), filename=str(migration))
    downgrade = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "downgrade")
    dropped_tables: list[str] = []

    for node in ast.walk(downgrade):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "drop_table":
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            dropped_tables.append(str(node.args[0].value))

    assert "app_users" not in dropped_tables
    assert "user_token_ledgers" not in dropped_tables


@pytest.mark.no_postgres
def test_ai_group_quality_migration_generates_offline_sql() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0070:0071", "--sql"],
        cwd=PROJECT_ROOT / "backend",
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE ai_group_message_memory" in result.stdout
