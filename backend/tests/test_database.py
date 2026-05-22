from configparser import ConfigParser
import ast
from pathlib import Path

from app.database import _escape_configparser_value


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
