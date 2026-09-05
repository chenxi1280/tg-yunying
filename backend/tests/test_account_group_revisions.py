import pytest
from sqlalchemy import func, select

from app.models import (
    AccountGroupStateRevision, AccountPool,
    StageWakeOutbox, Tenant, TgAccount,
)
from app.services.account_group_revisions import (
    begin_membership_change, finish_membership_change, initialize_group_revisions,
)
from app.services.account_group_revision_snapshot import current_group_revisions
from app.services.task_center.runtime_state_hash import canonical_state_hash
from tests.test_engagement_runtime_resources import _session


pytestmark = pytest.mark.no_postgres


def _seed(session):
    session.add(Tenant(id=1, name="member test"))
    session.flush()
    session.add_all([AccountPool(id=1, tenant_id=1, name="source"),
        AccountPool(id=2, tenant_id=1, name="target")])
    session.flush()
    session.add_all([TgAccount(id=i, tenant_id=1, pool_id=1, display_name=f"test-{i}",
        phone_masked="test") for i in (12, 11)])
    session.flush()


def _initialize(session):
    return initialize_group_revisions(session, 1, (2, 1), actor="test", reason="bootstrap")


def test_initial_membership_is_canonical_and_repeated_snapshot_is_noop():
    with _session() as session:
        _seed(session)
        first = _initialize(session)
        second = _initialize(session)
        assert [pair.versions for pair in first] == [(1, 1), (1, 1)]
        assert first[0].membership.member_account_ids == [11, 12]
        assert first[0].membership.member_set_hash == canonical_state_hash([11, 12])
        assert first[0].membership.reason == "bootstrap"
        assert [pair.membership.id for pair in first] == [pair.membership.id for pair in second]
        assert session.scalar(select(func.count(StageWakeOutbox.id))) == 4


def test_move_appends_both_groups_without_rewriting_original_evidence():
    with _session() as session:
        _seed(session)
        original = _initialize(session)
        token = begin_membership_change(session, 1, (1, 2), actor="operator", reason="move",
            expected_versions={1: (1, 1), 2: (1, 1)})
        session.get(TgAccount, 11).pool_id = 2
        changed = finish_membership_change(session, token)
        assert [pair.versions for pair in changed] == [(2, 1), (2, 1)]
        assert changed[0].membership.member_account_ids == [12]
        assert changed[1].membership.member_account_ids == [11]
        assert original[0].membership.member_account_ids == [11, 12]
        assert original[1].membership.member_account_ids == []
        assert changed[0].membership.supersedes_revision_id == original[0].membership.id


def test_rename_and_runtime_health_do_not_change_membership_revision():
    with _session() as session:
        _seed(session)
        _initialize(session)
        token = begin_membership_change(session, 1, (1,), actor="operator", reason="rename")
        session.get(AccountPool, 1).name = "new name"
        session.get(TgAccount, 11).health_score = 10
        changed, = finish_membership_change(session, token)
        assert changed.versions == (1, 1)


def test_disabling_group_keeps_member_evidence_and_appends_state():
    with _session() as session:
        _seed(session)
        _initialize(session)
        token = begin_membership_change(session, 1, (1,), actor="operator", reason="disable")
        session.get(AccountPool, 1).is_enabled = False
        changed, = finish_membership_change(session, token)
        assert changed.versions == (1, 2)
        assert changed.membership.member_account_ids == [11, 12]
        assert changed.state.group_state["is_enabled"] is False


def test_rollback_keeps_original_membership_and_wake_count():
    with _session() as session:
        _seed(session)
        _initialize(session)
        session.commit()
        token = begin_membership_change(session, 1, (1, 2), actor="operator", reason="move")
        session.get(TgAccount, 11).pool_id = 2
        finish_membership_change(session, token)
        session.rollback()
        assert session.get(TgAccount, 11).pool_id == 1
        assert [pair.versions for pair in current_group_revisions(session, 1, (1, 2))] == [(1, 1), (1, 1)]
        assert session.scalar(select(func.count(StageWakeOutbox.id))) == 4


def test_stale_expected_revision_rejects_without_mutation():
    with _session() as session:
        _seed(session)
        _initialize(session)
        with pytest.raises(ValueError, match="account_group_revision_conflict"):
            begin_membership_change(session, 1, (1,), actor="operator", reason="move",
                expected_versions={1: (0, 0)})
        assert session.get(TgAccount, 11).pool_id == 1


def test_outside_writer_drift_is_exposed_instead_of_silently_rebased():
    with _session() as session:
        _seed(session)
        _initialize(session)
        session.get(TgAccount, 11).pool_id = 2
        session.flush()
        with pytest.raises(ValueError, match="account_group_revision_drift"):
            begin_membership_change(session, 1, (1, 2), actor="operator", reason="move")


def test_token_detects_modified_original_revision_in_same_identity_map():
    with _session() as session:
        _seed(session)
        token = begin_membership_change(session, 1, (1,), actor="operator", reason="move")
        token.before[0].membership.member_account_ids = []
        with pytest.raises(ValueError, match="account_group_revision_conflict"):
            finish_membership_change(session, token)


def test_empty_deleted_group_keeps_historical_identity():
    with _session() as session:
        _seed(session)
        original = _initialize(session)[1]
        token = begin_membership_change(session, 1, (2,), actor="operator", reason="delete")
        session.delete(session.get(AccountPool, 2))
        changed, = finish_membership_change(session, token)
        assert changed.state.group_state["deleted"]
        assert changed.state.account_pool_id == original.state.account_pool_id == 2
        assert session.scalar(select(func.count(AccountGroupStateRevision.id))) == 3


def test_lock_refresh_preserves_pending_cosmetic_change():
    with _session() as session:
        _seed(session)
        _initialize(session)
        session.get(AccountPool, 1).name = "pending cosmetic name"
        change = begin_membership_change(session, 1, (1,), actor="test", reason="rename")
        pair, = finish_membership_change(session, change)
        assert pair.versions == (1, 1)
        assert session.get(AccountPool, 1).name == "pending cosmetic name"


def test_change_started_after_pending_semantic_edit_is_explicitly_rejected():
    with _session() as session:
        _seed(session)
        _initialize(session)
        session.get(AccountPool, 1).is_enabled = False
        with pytest.raises(ValueError, match="membership_change_started_after_mutation"):
            begin_membership_change(session, 1, (1,), actor="test", reason="too_late")
        assert session.get(AccountPool, 1).is_enabled is False
