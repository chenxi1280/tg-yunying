"""Batch admission must preserve per-account membership and dependency evidence."""
from types import SimpleNamespace
import pytest
from sqlalchemy import event, inspect

from app.models import AccountProxy, AiAccountVoiceProfile, OperationTarget, TgGroup, TgGroupAccount
from app.services.task_center import engagement_planning_admission as admission
from tests.test_engagement_participation import _account, _seed, _session


pytestmark = pytest.mark.no_postgres
MAX_ADMISSION_QUERIES = 7
QUALIFICATION_QUERIES = 5
ACCOUNT_COUNT = 20


def _seed_members(session):
    task = _seed(session)
    target = session.get(OperationTarget, 101)
    session.add(TgGroup(id=101, tenant_id=1, tg_peer_id=target.tg_peer_id, title="group"))
    for account_id in range(20, 20 + ACCOUNT_COUNT):
        account = _account(account_id)
        account.proxy = AccountProxy(tenant_id=1, name=f"proxy-{account_id}",
                                     port=1080, status="healthy", alert_status="normal")
        session.add(account)
        session.add(TgGroupAccount(tenant_id=1, group_id=101, account_id=account_id,
                                  can_send=account_id % 2 == 0))
    session.commit()
    return task, target


def test_admission_batches_queries_and_preserves_per_account_results():
    with _session() as session:
        task, target = _seed_members(session)
        ids = list(range(20, 20 + ACCOUNT_COUNT)) + [999]
        expected = [admission._planning_paths(session, task, [account_id],
                    target=target, require_send=True)[0] for account_id in ids]
        session.expire_all()
        statements = []
        connection = session.connection()
        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(connection, "before_cursor_execute", record)
        try:
            actual = admission._planning_paths(session, task, ids, target=target, require_send=True)
        finally:
            event.remove(connection, "before_cursor_execute", record)
        assert actual == expected
        assert [row["account_id"] for row in actual] == ids
        assert len(statements) <= MAX_ADMISSION_QUERIES + QUALIFICATION_QUERIES + 2  # expired Task and target
        ready = [row["account_id"] for row in actual if row["admissible"]]
        assert ready == [account_id for account_id in ids if account_id < 999 and account_id % 2 == 0]


def test_admission_only_loads_latest_profile_decision_fields():
    with _session() as session:
        task, _ = _seed_members(session)
        for version in (1, 2):
            session.add(AiAccountVoiceProfile(tenant_id=1, account_id=20, version=version,
                status="active" if version == 1 else "disabled", short_prompt_summary="summary",
                persona_experiences=["large historical detail" * 1000]))
        session.add(AiAccountVoiceProfile(tenant_id=2, account_id=20, version=3, status="active"))
        session.commit()
        session.expunge_all()
        task = SimpleNamespace(tenant_id=1, type="group_ai_chat")
        masks = admission._masks_by_account(session, task, [20])
        assert masks[20].version == 2 and masks[20].status == "disabled"
        assert "persona_experiences" in inspect(masks[20]).unloaded
        assert admission._mask_check(task, masks[20])["status"] == "deficit"
        assert len([row for row in session.identity_map.values() if isinstance(row, AiAccountVoiceProfile)]) == 1


def test_noncontent_admission_does_not_query_profiles(monkeypatch):
    with _session() as session:
        task, target = _seed_members(session)
        def unexpected(*args):
            pytest.fail("view admission queried content profiles")
        monkeypatch.setattr(admission, "_masks_by_account", unexpected)
        paths = admission._planning_paths(session, task, [20], target=target, require_send=False)
        assert paths[0]["account_id"] == 20
