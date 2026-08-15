"""Allow the built-in administrator as a batch-login recipient.

Revision ID: 0149_batch_login_principal
Revises: 0148_account_batch_login
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0149_batch_login_principal"
down_revision = "0148_account_batch_login"
branch_labels = None
depends_on = None


RECIPIENT_TABLES = (
    ("tg_account_login_batches", "fk_login_batch_recipient_app_user"),
    ("tg_account_login_batch_notifications", "fk_login_batch_notification_recipient_app_user"),
)


def _recipient_foreign_key(table_name: str) -> str | None:
    foreign_keys = sa.inspect(op.get_bind()).get_foreign_keys(table_name)
    for foreign_key in foreign_keys:
        if (
            foreign_key.get("constrained_columns") == ["recipient_user_id"]
            and foreign_key.get("referred_table") == "app_users"
        ):
            return str(foreign_key["name"])
    return None


def upgrade() -> None:
    for table_name, _ in RECIPIENT_TABLES:
        constraint_name = _recipient_foreign_key(table_name)
        if constraint_name:
            op.drop_constraint(constraint_name, table_name, type_="foreignkey")


def downgrade() -> None:
    connection = op.get_bind()
    app_users = sa.table("app_users", sa.column("id", sa.Integer()))
    for table_name, _ in RECIPIENT_TABLES:
        recipients = sa.table(table_name, sa.column("recipient_user_id", sa.Integer()))
        orphan_count = connection.execute(
            sa.select(sa.func.count())
            .select_from(recipients.outerjoin(app_users, app_users.c.id == recipients.c.recipient_user_id))
            .where(app_users.c.id.is_(None))
        ).scalar_one()
        if orphan_count:
            raise RuntimeError(f"cannot restore app_users recipient foreign key for {table_name}")
    for table_name, constraint_name in RECIPIENT_TABLES:
        if not _recipient_foreign_key(table_name):
            op.create_foreign_key(
                constraint_name,
                table_name,
                "app_users",
                ["recipient_user_id"],
                ["id"],
            )
