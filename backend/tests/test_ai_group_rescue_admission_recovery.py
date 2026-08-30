from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ai_group_rescue_recovery_test_support import (
    _membership_observation,
    _recovery_scope,
    _seed_base,
    _seed_recovery_accounts,
    _seed_unknown_rescue,
)
from app.database import Base
from app.models import (
    AccountGroupAdmissionFact,
    Action,
    Task,
    TaskAccountDailyCoverage,
    TgGroupAccount,
)
from app.services.task_center.ai_group_rescue_protected_recovery import (
    apply_admission_recovery,
    preview_admission_recovery,
)


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        _seed_base(current)
        current.commit()
        yield current


def test_member_observation_writes_fact_without_replaying_old_action(
    session: Session,
) -> None:
    old, item, coverage = _seed_unknown_rescue(session)
    coverage.state = "unknown"
    coverage.blocker_code = "membership_permission_denied"
    historical = TaskAccountDailyCoverage(
        tenant_id=1,
        task_id="task-1",
        group_id=11,
        account_id=102,
        membership_item_id=item.id,
        coverage_date=coverage.coverage_date - timedelta(days=1),
        state="blocked",
        blocker_code="historical_membership_unknown",
    )
    session.add(historical)
    old_snapshot = (old.status, dict(old.payload), dict(old.result))
    task = session.get(Task, "task-1")
    task.type_config = {
        **dict(task.type_config or {}),
        "group_rescue_admin_account_id": 103,
    }
    _seed_recovery_accounts(session)
    session.flush()
    observation = _membership_observation("member")
    preview = preview_admission_recovery(
        session,
        _recovery_scope(config_revision=1),
        (observation,),
    )

    result = apply_admission_recovery(
        session,
        _recovery_scope(config_revision=1),
        (observation,),
        expected_fingerprint=preview["fingerprint"],
        actor="operator",
        approval_reference="incident-1",
    )

    assert result["member_count"] == 1
    assert (old.status, old.payload, old.result) == old_snapshot
    assert session.query(AccountGroupAdmissionFact).count() == 1
    assert session.query(TgGroupAccount).filter_by(account_id=102).count() == 1
    assert item.phase == "completed"
    assert coverage.state == "ready"
    assert historical.state == "blocked"


def test_member_observation_accepts_legacy_unbound_membership_item(
    session: Session,
) -> None:
    old, item, coverage = _seed_unknown_rescue(session)
    item.rescue_action_id = None
    task = session.get(Task, "task-1")
    task.type_config = {
        **dict(task.type_config or {}),
        "group_rescue_admin_account_id": 103,
    }
    _seed_recovery_accounts(session)
    session.flush()
    observation = _membership_observation("member")
    scope = _recovery_scope(config_revision=1)
    preview = preview_admission_recovery(session, scope, (observation,))

    result = apply_admission_recovery(
        session,
        scope,
        (observation,),
        expected_fingerprint=preview["fingerprint"],
        actor="operator",
        approval_reference="incident-1",
    )

    assert result["member_count"] == 1
    assert old.status == "closed_unknown"
    assert item.rescue_action_id is None
    assert item.phase == "completed"
    assert coverage.state == "ready"


def test_member_observation_rejects_unrelated_membership_action(
    session: Session,
) -> None:
    _old, item, _coverage = _seed_unknown_rescue(session)
    item.rescue_action_id = "unrelated-action"
    task = session.get(Task, "task-1")
    task.type_config = {
        **dict(task.type_config or {}),
        "group_rescue_admin_account_id": 103,
    }
    _seed_recovery_accounts(session)
    session.flush()

    with pytest.raises(RuntimeError, match="membership_item_drift"):
        preview_admission_recovery(
            session,
            _recovery_scope(config_revision=1),
            (_membership_observation("member"),),
        )


def test_absent_observation_creates_unique_replacement_action(
    session: Session,
) -> None:
    old, item, _coverage = _seed_unknown_rescue(session)
    task = session.get(Task, "task-1")
    task.type_config = {
        **dict(task.type_config or {}),
        "group_rescue_admin_account_id": 103,
    }
    _seed_recovery_accounts(session)
    session.flush()
    observation = _membership_observation("absent")
    scope = _recovery_scope(config_revision=1)
    preview = preview_admission_recovery(session, scope, (observation,))

    first = apply_admission_recovery(
        session,
        scope,
        (observation,),
        expected_fingerprint=preview["fingerprint"],
        actor="operator",
        approval_reference="incident-1",
    )
    second_preview = preview_admission_recovery(session, scope, (observation,))
    second = apply_admission_recovery(
        session,
        scope,
        (observation,),
        expected_fingerprint=second_preview["fingerprint"],
        actor="operator",
        approval_reference="incident-1",
    )

    replacements = list(session.scalars(select(Action).where(
        Action.action_type == "invite_group_account",
        Action.id != old.id,
    )))
    assert first["replacement_count"] == 1
    assert second["replacement_count"] == 0
    assert len(replacements) == 1
    assert replacements[0].account_id == 103
    assert replacements[0].id == item.rescue_action_id
    assert replacements[0].result["recovery_source_action_id"] == old.id
    assert replacements[0].result["recovery_evidence_fingerprint"] == "evidence-absent"
    assert old.status == "closed_unknown"


def test_inconclusive_observation_preserves_unknown_state(
    session: Session,
) -> None:
    old, item, coverage = _seed_unknown_rescue(session)
    task = session.get(Task, "task-1")
    task.type_config = {
        **dict(task.type_config or {}),
        "group_rescue_admin_account_id": 103,
    }
    _seed_recovery_accounts(session)
    session.flush()
    observation = _membership_observation("inconclusive")
    scope = _recovery_scope(config_revision=1)
    preview = preview_admission_recovery(session, scope, (observation,))

    result = apply_admission_recovery(
        session,
        scope,
        (observation,),
        expected_fingerprint=preview["fingerprint"],
        actor="operator",
        approval_reference="incident-1",
    )

    assert result["inconclusive_count"] == 1
    assert session.query(AccountGroupAdmissionFact).count() == 0
    assert session.query(Action).filter(Action.id != old.id).count() == 0
    assert item.phase == "failed"
    assert item.rescue_action_id == old.id
    assert coverage.state == "blocked"
    assert old.status == "closed_unknown"


def test_admission_apply_rejects_preview_fingerprint_drift(
    session: Session,
) -> None:
    old, _item, _coverage = _seed_unknown_rescue(session)
    task = session.get(Task, "task-1")
    task.type_config = {
        **dict(task.type_config or {}),
        "group_rescue_admin_account_id": 103,
    }
    _seed_recovery_accounts(session)
    session.flush()
    observation = _membership_observation("member")
    scope = _recovery_scope(config_revision=1)
    preview = preview_admission_recovery(session, scope, (observation,))
    old.status = "skipped"
    session.flush()

    with pytest.raises(RuntimeError, match="fingerprint_drift"):
        apply_admission_recovery(
            session,
            scope,
            (observation,),
            expected_fingerprint=preview["fingerprint"],
            actor="operator",
            approval_reference="incident-1",
        )


def test_admission_preview_rejects_source_peer_drift(
    session: Session,
) -> None:
    old, _item, _coverage = _seed_unknown_rescue(session)
    old.payload = {**dict(old.payload), "group_peer_id": "-100999"}
    task = session.get(Task, "task-1")
    task.type_config = {
        **dict(task.type_config or {}),
        "group_rescue_admin_account_id": 103,
    }
    _seed_recovery_accounts(session)
    session.flush()

    with pytest.raises(RuntimeError, match="source_peer_drift"):
        preview_admission_recovery(
            session,
            _recovery_scope(config_revision=1),
            (_membership_observation("member"),),
        )
