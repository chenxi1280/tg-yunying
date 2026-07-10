from __future__ import annotations

from dataclasses import FrozenInstanceError
from importlib import import_module

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountPool, Tenant, TgAccount


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([Tenant(id=1, name="tenant-1"), Tenant(id=2, name="tenant-2")])
        db.add_all(
            [
                AccountPool(id=10, tenant_id=1, name="normal", pool_purpose="normal"),
                AccountPool(id=11, tenant_id=1, name="receiver", pool_purpose="code_receiver"),
                AccountPool(id=12, tenant_id=1, name="rank", pool_purpose="rank_deboost"),
                AccountPool(id=13, tenant_id=1, name="disabled", pool_purpose="normal", is_enabled=False),
                AccountPool(id=14, tenant_id=1, name="legacy-special", pool_purpose="normal", system_key="rank_deboost"),
                AccountPool(id=20, tenant_id=2, name="other", pool_purpose="normal"),
            ]
        )
        db.commit()
        yield db


def _policy():
    return import_module("app.services.account_usage_policy")


def _account(account_id: int, pool_id: int | None, identity: str, tenant_id: int = 1) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=tenant_id,
        pool_id=pool_id,
        display_name=f"account-{account_id}",
        phone_masked=str(account_id),
        account_identity=identity,
    )


@pytest.mark.parametrize(
    ("pool_id", "identity", "expected"),
    [(10, "normal", "normal"), (11, "code_receiver", "code_receiver"), (12, "rank_deboost", "rank_deboost")],
)
def test_account_usage_uses_pool_purpose_as_truth(
    session: Session,
    pool_id: int,
    identity: str,
    expected: str,
) -> None:
    account = _account(100 + pool_id, pool_id, identity)
    assert _policy().account_usage(account, session.get(AccountPool, pool_id)) == expected


@pytest.mark.parametrize(
    ("pool_id", "identity"),
    [(None, "normal"), (10, "rank_deboost"), (20, "normal")],
)
def test_account_usage_marks_missing_cross_tenant_and_projection_conflicts_as_mismatch(
    session: Session,
    pool_id: int | None,
    identity: str,
) -> None:
    pool = session.get(AccountPool, pool_id) if pool_id else None
    assert _policy().account_usage(_account(200, pool_id, identity), pool) == "mismatch"


@pytest.mark.parametrize(
    "action_kind",
    [
        "login",
        "relogin",
        "authorization_diagnostics",
        "standby_session_repair",
        "readonly_device_diagnostics",
        "account_health_probe",
        "official_verification_code_read",
    ],
)
def test_authorization_asset_actions_allow_every_consistent_usage(session: Session, action_kind: str) -> None:
    policy = _policy()
    for pool_id, identity in [(10, "normal"), (11, "code_receiver"), (12, "rank_deboost")]:
        policy.assert_account_action_allowed(_account(pool_id, pool_id, identity), session.get(AccountPool, pool_id), action_kind)


@pytest.mark.parametrize(
    "action_kind",
    ["operational_task", "profile_update", "account_mask_init", "two_fa_rotate", "device_cleanup"],
)
def test_specialized_accounts_cannot_run_operational_profile_or_security_mutations(
    session: Session,
    action_kind: str,
) -> None:
    policy = _policy()
    policy.assert_account_action_allowed(_account(300, 10, "normal"), session.get(AccountPool, 10), action_kind)
    for pool_id, identity in [(11, "code_receiver"), (12, "rank_deboost")]:
        with pytest.raises(ValueError, match="account_action_not_allowed"):
            policy.assert_account_action_allowed(_account(pool_id, pool_id, identity), session.get(AccountPool, pool_id), action_kind)


def test_rank_deboost_action_is_exclusive_and_mismatch_blocks_every_external_action(session: Session) -> None:
    policy = _policy()
    policy.assert_account_action_allowed(_account(312, 12, "rank_deboost"), session.get(AccountPool, 12), "search_rank_deboost")
    for pool_id, identity in [(10, "normal"), (11, "code_receiver")]:
        with pytest.raises(ValueError, match="account_action_not_allowed"):
            policy.assert_account_action_allowed(_account(pool_id, pool_id, identity), session.get(AccountPool, pool_id), "search_rank_deboost")
    with pytest.raises(ValueError, match="account_purpose_mismatch"):
        policy.assert_account_action_allowed(_account(399, 10, "rank_deboost"), session.get(AccountPool, 10), "login")


def test_account_filters_require_enabled_same_tenant_pool_and_matching_projection(session: Session) -> None:
    session.add_all(
        [
            _account(401, 10, "normal"),
            _account(402, 12, "rank_deboost"),
            _account(403, 10, "rank_deboost"),
            _account(404, 13, "normal"),
            _account(405, 20, "normal"),
            _account(406, None, "normal"),
            _account(407, 14, "normal"),
        ]
    )
    session.commit()
    policy = _policy()
    operational = session.scalars(policy.apply_operational_account_filters(select(TgAccount))).all()
    rank = session.scalars(policy.apply_rank_deboost_account_filters(select(TgAccount))).all()
    assert {item.id for item in operational} == {401}
    assert {item.id for item in rank} == {402}


def test_sync_account_usage_updates_projection_atomically_and_returns_frozen_summary(session: Session) -> None:
    account = _account(500, 10, "normal")
    session.add(account)
    session.commit()
    summary = _policy().sync_account_usage(session, account, session.get(AccountPool, 12), "tester")
    assert (account.pool_id, account.account_identity) == (12, "rank_deboost")
    assert (summary.previous_pool_id, summary.target_pool_id, summary.usage) == (10, 12, "rank_deboost")
    with pytest.raises(FrozenInstanceError):
        summary.usage = "normal"
    session.rollback()
    session.refresh(account)
    assert (account.pool_id, account.account_identity) == (10, "normal")


@pytest.mark.parametrize("pool_id", [13, 20])
def test_sync_account_usage_rejects_disabled_or_cross_tenant_pool(session: Session, pool_id: int) -> None:
    account = _account(600 + pool_id, 10, "normal")
    session.add(account)
    session.commit()
    with pytest.raises(ValueError):
        _policy().sync_account_usage(session, account, session.get(AccountPool, pool_id), "tester")
    assert (account.pool_id, account.account_identity) == (10, "normal")
