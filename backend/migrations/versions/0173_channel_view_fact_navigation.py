"""Repair channel-view fact navigation after the released 0172 revision.

Revision ID: 0173_channel_view_fact_nav
Revises: 0172_channel_view_daily_fact
"""

from alembic import op
import sqlalchemy as sa


revision = "0173_channel_view_fact_nav"
down_revision = "0172_channel_view_daily_fact"
branch_labels = None
depends_on = None

TABLE = "view_remote_facts"
COLUMN = "obligation_id"
TARGET_FK = "fk_view_remote_fact_obligation_navigation"
LEGACY_FK = "view_remote_facts_obligation_id_fkey"
REFERRED_TABLE = "view_fulfillment_obligations"


def upgrade() -> None:
    _assert_navigation_column_exists()
    if _contract_matches(nullable=True, ondelete="SET NULL", name=TARGET_FK):
        return
    _drop_navigation_foreign_keys()
    op.alter_column(TABLE, COLUMN, existing_type=sa.String(length=36), nullable=True)
    _create_navigation_foreign_key(name=TARGET_FK, ondelete="SET NULL")
    _assert_contract(nullable=True, ondelete="SET NULL", name=TARGET_FK)


def downgrade() -> None:
    _assert_navigation_column_exists()
    unsafe_count = _unsafe_navigation_count()
    if unsafe_count:
        raise RuntimeError(
            f"channel_view_fact_navigation_downgrade_unsafe:{unsafe_count}"
        )
    _drop_navigation_foreign_keys()
    op.alter_column(TABLE, COLUMN, existing_type=sa.String(length=36), nullable=False)
    _create_navigation_foreign_key(name=LEGACY_FK, ondelete="CASCADE")
    _assert_contract(nullable=False, ondelete="CASCADE", name=LEGACY_FK)


def _assert_navigation_column_exists() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(TABLE)}
    if COLUMN not in columns:
        raise RuntimeError("channel_view_fact_navigation_column_missing")


def _drop_navigation_foreign_keys() -> None:
    for foreign_key in _navigation_foreign_keys():
        name = foreign_key.get("name")
        if not name:
            raise RuntimeError("channel_view_fact_navigation_fk_unnamed")
        op.drop_constraint(name, TABLE, type_="foreignkey")


def _create_navigation_foreign_key(*, name: str, ondelete: str) -> None:
    op.create_foreign_key(
        name,
        TABLE,
        REFERRED_TABLE,
        [COLUMN],
        ["id"],
        ondelete=ondelete,
    )


def _unsafe_navigation_count() -> int:
    result = op.get_bind().execute(sa.text("""
        SELECT COUNT(*)
        FROM view_remote_facts fact
        LEFT JOIN view_fulfillment_obligations obligation
          ON obligation.id = fact.obligation_id
        WHERE fact.obligation_id IS NULL OR obligation.id IS NULL
    """))
    return int(result.scalar_one())


def _assert_contract(*, nullable: bool, ondelete: str, name: str) -> None:
    if _contract_matches(nullable=nullable, ondelete=ondelete, name=name):
        return
    raise RuntimeError("channel_view_fact_navigation_contract_mismatch")


def _contract_matches(*, nullable: bool, ondelete: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"]: column for column in inspector.get_columns(TABLE)}
    foreign_keys = _navigation_foreign_keys()
    return (
        bool(columns[COLUMN]["nullable"]) is nullable
        and len(foreign_keys) == 1
        and foreign_keys[0].get("name") == name
        and _ondelete(foreign_keys[0]) == ondelete
    )


def _navigation_foreign_keys() -> list[dict]:
    return [
        foreign_key
        for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(TABLE)
        if foreign_key.get("constrained_columns") == [COLUMN]
    ]


def _ondelete(foreign_key: dict) -> str:
    return str((foreign_key.get("options") or {}).get("ondelete") or "").upper()
