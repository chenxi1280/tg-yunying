from __future__ import annotations
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[1]
DATABASE_URL = get_settings().database_url


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
if DATABASE_URL.startswith("postgresql"):
    connect_args = {"options": "-c timezone=utc"}
engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def is_sqlite_url(url: str | None = None) -> bool:
    return (url or DATABASE_URL).startswith("sqlite")


def run_migrations() -> None:
    """Apply Alembic migrations for the configured database."""
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(alembic_cfg, "head")


def ensure_schema_compat() -> None:
    """Keep local SQLite prototypes compatible with additive model changes."""
    if not is_sqlite_url():
        return
    with engine.begin() as connection:
        rows = connection.execute(text("PRAGMA table_info(app_users)")).mappings().all()
        columns = {row["name"] for row in rows}
        if rows and "password_hash" not in columns:
            connection.execute(text("ALTER TABLE app_users ADD COLUMN password_hash VARCHAR(240) DEFAULT ''"))
        for column_name, ddl in {
            "phone": "VARCHAR(40)",
            "subscription_status": "VARCHAR(30) DEFAULT 'active' NOT NULL",
            "subscription_started_at": "DATETIME",
            "subscription_expires_at": "DATETIME",
            "last_activated_at": "DATETIME",
            "created_at": "DATETIME",
            "last_login_at": "DATETIME",
        }.items():
            if rows and column_name not in columns:
                connection.execute(text(f"ALTER TABLE app_users ADD COLUMN {column_name} {ddl}"))

        account_rows = connection.execute(text("PRAGMA table_info(tg_accounts)")).mappings().all()
        account_columns = {row["name"] for row in account_rows}
        if account_rows and "phone_ciphertext" not in account_columns:
            connection.execute(text("ALTER TABLE tg_accounts ADD COLUMN phone_ciphertext TEXT"))
        if account_rows and "developer_app_id" not in account_columns:
            connection.execute(text("ALTER TABLE tg_accounts ADD COLUMN developer_app_id INTEGER"))
        if account_rows and "developer_app_version" not in account_columns:
            connection.execute(text("ALTER TABLE tg_accounts ADD COLUMN developer_app_version INTEGER DEFAULT 1 NOT NULL"))
        for column_name, ddl in {
            "tg_first_name": "VARCHAR(80) DEFAULT '' NOT NULL",
            "tg_last_name": "VARCHAR(80) DEFAULT '' NOT NULL",
            "tg_bio": "TEXT DEFAULT '' NOT NULL",
            "avatar_object_key": "VARCHAR(300) DEFAULT '' NOT NULL",
            "profile_sync_status": "VARCHAR(30) DEFAULT '未同步' NOT NULL",
            "profile_sync_error": "TEXT DEFAULT '' NOT NULL",
            "profile_synced_at": "DATETIME",
        }.items():
            if account_rows and column_name not in account_columns:
                connection.execute(text(f"ALTER TABLE tg_accounts ADD COLUMN {column_name} {ddl}"))

        campaign_rows = connection.execute(text("PRAGMA table_info(campaigns)")).mappings().all()
        campaign_columns = {row["name"] for row in campaign_rows}
        for column_name, ddl in {
            "ai_provider_id": "INTEGER",
            "prompt_template_id": "INTEGER",
            "jitter_min_seconds": "INTEGER",
            "jitter_max_seconds": "INTEGER",
            "batch_interval_seconds": "INTEGER",
            "respect_send_window": "BOOLEAN",
            "material_ids": "TEXT DEFAULT '' NOT NULL",
            "target_group_ids": "TEXT DEFAULT '' NOT NULL",
            "selected_account_ids_by_group": "TEXT DEFAULT '' NOT NULL",
        }.items():
            if campaign_rows and column_name not in campaign_columns:
                connection.execute(text(f"ALTER TABLE campaigns ADD COLUMN {column_name} {ddl}"))

        draft_rows = connection.execute(text("PRAGMA table_info(ai_drafts)")).mappings().all()
        draft_columns = {row["name"] for row in draft_rows}
        for column_name, ddl in {
            "provider_name": "VARCHAR(100) DEFAULT 'Mock' NOT NULL",
            "model_name": "VARCHAR(120) DEFAULT 'mock' NOT NULL",
            "prompt_template_name": "VARCHAR(120) DEFAULT '默认模板' NOT NULL",
            "material_id": "INTEGER",
            "suggested_account_id": "INTEGER",
            "sequence_index": "INTEGER DEFAULT 0 NOT NULL",
            "reply_to_draft_id": "INTEGER",
            "generation_source": "VARCHAR(40) DEFAULT 'mock' NOT NULL",
            "generation_error": "TEXT DEFAULT '' NOT NULL",
        }.items():
            if draft_rows and column_name not in draft_columns:
                connection.execute(text(f"ALTER TABLE ai_drafts ADD COLUMN {column_name} {ddl}"))

        task_rows = connection.execute(text("PRAGMA table_info(message_tasks)")).mappings().all()
        task_columns = {row["name"] for row in task_rows}
        for column_name, ddl in {
            "message_type": "VARCHAR(40) DEFAULT '文本' NOT NULL",
            "material_id": "INTEGER",
            "planned_delay_seconds": "INTEGER DEFAULT 0 NOT NULL",
            "target_type": "VARCHAR(30) DEFAULT 'group' NOT NULL",
            "target_peer_id": "VARCHAR(120)",
            "target_display": "VARCHAR(160) DEFAULT '' NOT NULL",
            "preferred_account_id": "INTEGER",
        }.items():
            if task_rows and column_name not in task_columns:
                connection.execute(text(f"ALTER TABLE message_tasks ADD COLUMN {column_name} {ddl}"))

        provider_rows = connection.execute(text("PRAGMA table_info(ai_providers)")).mappings().all()
        provider_columns = {row["name"] for row in provider_rows}
        for column_name, ddl in {
            "input_price_per_1k": "FLOAT DEFAULT 0 NOT NULL",
            "output_price_per_1k": "FLOAT DEFAULT 0 NOT NULL",
            "currency": "VARCHAR(16) DEFAULT 'CNY' NOT NULL",
            "is_billable": "BOOLEAN DEFAULT 1 NOT NULL",
        }.items():
            if provider_rows and column_name not in provider_columns:
                connection.execute(text(f"ALTER TABLE ai_providers ADD COLUMN {column_name} {ddl}"))

        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS tg_contacts (
                    id INTEGER PRIMARY KEY,
                    tenant_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    peer_id VARCHAR(120) NOT NULL,
                    display_name VARCHAR(160) NOT NULL,
                    username VARCHAR(120),
                    phone_masked VARCHAR(60) DEFAULT '' NOT NULL,
                    contact_type VARCHAR(40) DEFAULT 'private' NOT NULL,
                    is_mutual BOOLEAN DEFAULT 0 NOT NULL,
                    last_message_at DATETIME,
                    last_synced_at DATETIME NOT NULL,
                    created_at DATETIME NOT NULL,
                    UNIQUE (account_id, peer_id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS activation_codes (
                    id INTEGER PRIMARY KEY,
                    code VARCHAR(64) NOT NULL UNIQUE,
                    plan_type VARCHAR(30) NOT NULL,
                    duration_days INTEGER NOT NULL,
                    status VARCHAR(30) DEFAULT 'unused' NOT NULL,
                    created_by VARCHAR(100) DEFAULT '' NOT NULL,
                    created_at DATETIME NOT NULL,
                    redeemed_by_user_id INTEGER,
                    redeemed_at DATETIME,
                    subscription_start_at DATETIME,
                    subscription_end_at DATETIME,
                    note VARCHAR(255) DEFAULT '' NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS ai_usage_ledgers (
                    id INTEGER PRIMARY KEY,
                    tenant_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    campaign_id INTEGER,
                    group_id INTEGER,
                    provider_id INTEGER,
                    provider_name VARCHAR(100) DEFAULT '' NOT NULL,
                    model_name VARCHAR(120) DEFAULT '' NOT NULL,
                    prompt_template_id INTEGER,
                    request_type VARCHAR(60) DEFAULT 'campaign_draft_generation' NOT NULL,
                    prompt_tokens INTEGER DEFAULT 0 NOT NULL,
                    completion_tokens INTEGER DEFAULT 0 NOT NULL,
                    total_tokens INTEGER DEFAULT 0 NOT NULL,
                    input_unit_price FLOAT DEFAULT 0 NOT NULL,
                    output_unit_price FLOAT DEFAULT 0 NOT NULL,
                    total_cost FLOAT DEFAULT 0 NOT NULL,
                    currency VARCHAR(16) DEFAULT 'CNY' NOT NULL,
                    billable BOOLEAN DEFAULT 0 NOT NULL,
                    request_status VARCHAR(30) DEFAULT 'success' NOT NULL,
                    error_detail TEXT DEFAULT '' NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        archive_rows = connection.execute(text("PRAGMA table_info(group_archives)")).mappings().all()
        archive_columns = {row["name"] for row in archive_rows}
        for column_name, ddl in {
            "sync_mode": "VARCHAR(30) DEFAULT 'sync' NOT NULL",
            "failure_detail": "TEXT DEFAULT '' NOT NULL",
        }.items():
            if archive_rows and column_name not in archive_columns:
                connection.execute(text(f"ALTER TABLE group_archives ADD COLUMN {column_name} {ddl}"))
