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
    "tg_account_full_initializations",
    "tg_account_login_post_init_bindings",
    "tg_post_login_abc_requests",
    "tg_authorization_online_abc_batches",
    "tg_authorization_online_abc_items",
    "tg_authorization_online_abc_slot_results",
}


def _foreign_key_names(inspector, table_name: str) -> dict[tuple[str, ...], str | None]:
    return {
        tuple(foreign_key["constrained_columns"]): foreign_key["name"]
        for foreign_key in inspector.get_foreign_keys(table_name)
    }


def _assert_account_login_schema(inspector) -> None:
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
    batch_columns = {column["name"] for column in inspector.get_columns("tg_account_login_batches")}
    assert {
        "authorized_count",
        "fully_initialized_count",
        "post_init_waiting_count",
        "manual_required_count",
        "initialization_policy",
    } <= batch_columns
    item_columns = {column["name"] for column in inspector.get_columns("tg_account_login_batch_items")}
    assert {
        "authorization_status",
        "post_initialization_id",
        "post_initialization_status",
        "post_initialization_failure_type",
        "initialization_policy",
    } <= item_columns
    item_indexes = {index["name"] for index in inspector.get_indexes("tg_account_login_batch_items")}
    assert "ux_login_batch_item_account" in item_indexes
    account_indexes = {
        index["name"]: index for index in inspector.get_indexes("tg_accounts")
    }
    assert "ux_tg_accounts_tenant_phone_active" not in account_indexes
    assert account_indexes["ix_tg_accounts_tenant_phone_masked_active"]["unique"] is False
    full_init_columns = {
        column["name"] for column in inspector.get_columns("tg_account_full_initializations")
    }
    assert {"abc_evidence_ref", "two_fa_next_retry_at"} <= full_init_columns
    assert _foreign_key_names(inspector, "tg_account_login_batch_items")[("current_attempt_id",)] == (
        "fk_login_batch_item_current_attempt"
    )
    attempt_foreign_keys = _foreign_key_names(inspector, "tg_account_login_batch_attempts")
    assert attempt_foreign_keys[("item_id",)] == "fk_login_attempt_item"
    assert attempt_foreign_keys[("flow_id",)] == "fk_login_attempt_flow"
    assert _foreign_key_names(inspector, "tg_login_flows")[("batch_login_attempt_id",)] == (
        "fk_login_flow_batch_attempt"
    )


def _assert_ai_schema(inspector) -> None:
    assert ("recipient_user_id",) not in _foreign_key_names(inspector, "tg_account_login_batches")
    assert ("recipient_user_id",) not in _foreign_key_names(
        inspector, "tg_account_login_batch_notifications"
    )
    usage_columns = {column["name"]: column for column in inspector.get_columns("ai_usage_ledgers")}
    assert usage_columns["user_id"]["nullable"] is False
    ai_setting_columns = {
        column["name"] for column in inspector.get_columns("tenant_ai_settings")
    }
    assert "ai_provider_route_fallback_enabled" in ai_setting_columns
    assert any(
        foreign_key["constrained_columns"] == ["user_id"]
        and foreign_key["referred_table"] == "app_users"
        for foreign_key in inspector.get_foreign_keys("ai_usage_ledgers")
    )
    attempt_columns = {
        column["name"]: column for column in inspector.get_columns("ai_provider_attempts")
    }
    assert attempt_columns["route_set_id"]["nullable"] is True


def test_account_batch_login_schema_migrates_from_blank_postgres() -> None:
    inspector = inspect(engine)

    assert EXPECTED_TABLES <= set(inspector.get_table_names())
    _assert_account_login_schema(inspector)
    _assert_ai_schema(inspector)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0224_legacy_account_occupancy"
        )
