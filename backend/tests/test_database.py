from configparser import ConfigParser

from app.database import _escape_configparser_value


def test_escape_configparser_value_allows_url_encoded_password() -> None:
    parser = ConfigParser()
    parser.add_section("alembic")
    database_url = "postgresql+psycopg://app_user:secret%2Bvalue@postgres:5432/tgyunying"

    parser.set("alembic", "sqlalchemy.url", _escape_configparser_value(database_url))

    assert parser.get("alembic", "sqlalchemy.url") == database_url
