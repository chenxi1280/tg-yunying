from copy import deepcopy

import pytest
from sqlalchemy import event, func, select

from app.models import AccountGroupMembershipRevision, AccountGroupStateRevision, AccountPool, StageWakeOutbox
from app.services.account_group_revision_bootstrap import (
    apply_group_revision_bootstrap, preview_group_revisions,
)
from tests.test_account_group_revisions import _initialize, _seed
from tests.test_engagement_runtime_resources import _session


pytestmark = pytest.mark.no_postgres


def _apply(session, preview):
    return apply_group_revision_bootstrap(session, preview, actor="test", audit_reference="test-bootstrap")


def test_preview_only_selects_and_does_not_initialize_missing_revisions():
    with _session() as session:
        _seed(session)
        connection = session.connection()
        operations = []
        def record(_connection, _cursor, statement, *_args):
            operations.append(statement.split()[0])
        event.listen(connection, "before_cursor_execute", record)
        try:
            preview = preview_group_revisions(session, 1)
        finally:
            event.remove(connection, "before_cursor_execute", record)
        assert set(operations) == {"SELECT"}
        assert [group["membership_revision"] for group in preview["state"]["groups"]] == [0, 0]
        assert session.scalar(select(func.count(AccountGroupMembershipRevision.id))) == 0


def test_bootstrap_applies_all_missing_groups_and_fresh_repeat_is_noop():
    with _session() as session:
        _seed(session)
        preview = preview_group_revisions(session, 1)
        receipt = _apply(session, preview)
        assert receipt["initialized_group_count"] == 2
        assert receipt["after_hash"] == preview_group_revisions(session, 1)["state_hash"]
        assert session.scalar(select(func.count(StageWakeOutbox.id))) == 4
        assert _apply(session, receipt["after"])["initialized_group_count"] == 0
        assert session.scalar(select(func.count(StageWakeOutbox.id))) == 4
        with pytest.raises(ValueError, match="account_group_bootstrap_preview_conflict"):
            _apply(session, preview)


def test_pool_added_after_preview_rejects_whole_apply_without_partial_baseline():
    with _session() as session:
        _seed(session)
        preview = preview_group_revisions(session, 1)
        session.add(AccountPool(id=3, tenant_id=1, name="新增组"))
        session.flush()
        with pytest.raises(ValueError, match="account_group_bootstrap_preview_conflict"):
            _apply(session, preview)
        assert session.scalar(select(func.count(AccountGroupMembershipRevision.id))) == 0


def test_bootstrap_rollback_removes_both_versions_and_all_wakes():
    with _session() as session:
        _seed(session)
        session.commit()
        _apply(session, preview_group_revisions(session, 1))
        session.rollback()
        assert session.scalar(select(func.count(AccountGroupMembershipRevision.id))) == 0
        assert session.scalar(select(func.count(AccountGroupStateRevision.id))) == 0
        assert session.scalar(select(func.count(StageWakeOutbox.id))) == 0


def test_existing_revision_drift_is_reported_and_cannot_be_rebased_by_bootstrap():
    with _session() as session:
        _seed(session)
        _initialize(session)
        session.get(AccountPool, 1).is_enabled = False
        session.flush()
        preview = preview_group_revisions(session, 1)
        assert preview["state"]["groups"][0]["issue"] == "account_group_revision_drift"
        with pytest.raises(ValueError, match="account_group_bootstrap_requires_drift_repair"):
            _apply(session, preview)


def test_tampered_preview_cannot_supply_an_arbitrary_approved_hash():
    with _session() as session:
        _seed(session)
        preview = deepcopy(preview_group_revisions(session, 1))
        preview["state"]["groups"] = []
        with pytest.raises(ValueError, match="account_group_bootstrap_preview_invalid"):
            _apply(session, preview)
