from __future__ import annotations

from sqlalchemy import inspect, text

from app.database import engine


EXPECTED_TABLES = {
    "tg_account_login_batches",
    "tg_account_login_batch_items",
    "tg_account_login_batch_attempts",
    "tg_account_login_batch_notifications",
    "tg_account_login_rate_buckets",
    "tg_account_phone_fingerprint_aliases",
}


def test_account_batch_login_schema_migrates_from_blank_postgres() -> None:
    inspector = inspect(engine)

    assert EXPECTED_TABLES <= set(inspector.get_table_names())
    account_columns = {column["name"] for column in inspector.get_columns("tg_accounts")}
    assert {
        "code_source_host",
        "code_source_uuid_ciphertext",
        "code_source_uuid_fingerprint",
        "code_source_uuid_hint",
        "code_source_binding_version",
    } <= account_columns
    flow_columns = {column["name"] for column in inspector.get_columns("tg_login_flows")}
    assert {"batch_login_attempt_id", "batch_login_generation"} <= flow_columns
    usage_columns = {column["name"]: column for column in inspector.get_columns("ai_usage_ledgers")}
    assert usage_columns["user_id"]["nullable"] is False
    assert any(
        foreign_key["constrained_columns"] == ["user_id"]
        and foreign_key["referred_table"] == "app_users"
        for foreign_key in inspector.get_foreign_keys("ai_usage_ledgers")
    )
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0148_account_batch_login"
        )
