import pytest
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.database import SessionLocal
from app.integrations.telegram import OperationResult
from app.models import AccountGroupAdmissionFact, Action, Task, TgAccount, TgGroup
from app.services.task_center import task_prejoin_channels as prejoin
from tests.test_hotpath_projections_postgres import scope, TENANT_ID, ACCOUNT_ID, TASK_ID

GROUP_ID = 926_915
ACTION_ID = "prejoin-pg-action"


@pytest.fixture
def prejoin_scope(scope):
    with SessionLocal.begin() as session:
        session.add(TgGroup(id=GROUP_ID, tenant_id=TENANT_ID, tg_peer_id="-100926915", title="t"))
        session.get(Task, TASK_ID).group_ai_prejoin_channel_ids = ["test_channel"]
        session.add(Action(id=ACTION_ID, tenant_id=TENANT_ID, task_id=TASK_ID,
            task_type="group_ai_chat", action_type="send_message", account_id=ACCOUNT_ID,
            status="executing", claim_owner="worker", claim_token="original"))
    yield
    with SessionLocal.begin() as session:
        session.execute(delete(AccountGroupAdmissionFact).where(
            AccountGroupAdmissionFact.tenant_id == TENANT_ID))
        session.execute(delete(TgGroup).where(TgGroup.id == GROUP_ID))


@pytest.mark.parametrize("replace_claim", [False, True])
def test_remote_wait_releases_real_transaction_and_account_lock(prejoin_scope, monkeypatch, replace_claim):
    observer = create_engine(SessionLocal.kw["bind"].url, poolclass=NullPool)
    with SessionLocal() as session:
        account = session.scalar(select(TgAccount).where(TgAccount.id == ACCOUNT_ID).with_for_update())
        pid = session.scalar(text("SELECT pg_backend_pid()"))
        action = session.get(Action, ACTION_ID)
        task = session.get(Task, TASK_ID)
        group = session.get(TgGroup, GROUP_ID)

        def remote(*args, **kwargs):
            assert not session.in_transaction()
            with Session(observer) as other:
                # Independent backend proves no retained transaction or row lock.
                state = other.execute(text("SELECT xact_start FROM pg_stat_activity WHERE pid=:pid"),
                                      {"pid": pid}).scalar_one()
                assert state is None
                other.scalar(select(TgAccount).where(TgAccount.id == ACCOUNT_ID).with_for_update(nowait=True))
                if replace_claim:
                    other.get(Action, ACTION_ID).claim_token = "new-owner"
                other.commit()
            return OperationResult(True, detail="already_joined")

        monkeypatch.setattr(prejoin.gateway, "ensure_channel_membership", remote)
        arguments = dict(task=task, action=action, account=account, credentials=object(), target_group=group)
        if replace_claim:
            with pytest.raises(prejoin.PrejoinOwnershipChanged):
                prejoin.ensure_prejoin_channels(session, **arguments)
        else:
            assert prejoin.ensure_prejoin_channels(session, **arguments)
            session.commit()
    with SessionLocal() as session:
        assert session.scalar(select(AccountGroupAdmissionFact).where(
            AccountGroupAdmissionFact.tenant_id == TENANT_ID)) is not None
        expected = "new-owner" if replace_claim else "original"
        assert session.get(Action, ACTION_ID).claim_token == expected
