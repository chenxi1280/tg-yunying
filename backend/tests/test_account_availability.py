from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountStatus, Tenant, TgAccount
from app.services.runtime_summary import get_account_runtime_summary, list_account_runtime_summaries, rebuild_runtime_summaries


def _sqlite_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_account_availability_rebuild_uses_summary_rows_not_live_detail_scan() -> None:
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(
                    id=11,
                    tenant_id=1,
                    display_name="在线账号",
                    phone_masked="11",
                    status=AccountStatus.ACTIVE.value,
                    session_ciphertext="session",
                    health_score=96,
                ),
                TgAccount(
                    id=12,
                    tenant_id=1,
                    display_name="缺 Session 账号",
                    phone_masked="12",
                    status=AccountStatus.ACTIVE.value,
                    session_ciphertext=None,
                    health_score=96,
                ),
            ]
        )
        session.commit()

        result = rebuild_runtime_summaries(session, 1, scope="accounts")
        rows = {row.account_id: row for row in list_account_runtime_summaries(session, 1)}
        missing_session = get_account_runtime_summary(session, 1, 12)

        assert result == {"tasks": 0, "targets": 0, "accounts": 2}
        assert rows[11].send_available is True
        assert rows[11].listen_available is True
        assert rows[12].send_available is False
        assert rows[12].unavailable_reason == "session_missing"
        assert missing_session.id == rows[12].id
