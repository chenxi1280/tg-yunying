from __future__ import annotations

import pytest
from sqlalchemy import select

import app.services.authorization_dr.online_abc_runner as runner
from app.models import TgAccount, TgAccountAuthorization, TgAuthorizationOnlineAbcItem
from tests import test_authorization_online_abc as abc_tests


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def db_session():
    fixture = abc_tests.session.__wrapped__()
    session = next(fixture)
    try:
        yield session
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass


def test_legacy_frozen_primary_is_qualified_before_b(db_session, monkeypatch) -> None:
    batch_id = abc_tests._apply(db_session, abc_tests._preview(db_session)["fingerprint"])["batch_id"]
    account, primary = _legacy_primary(db_session)
    calls: list[tuple[str, int]] = []
    abc_tests._mock_runner_success(monkeypatch, calls)

    def qualify(session, tenant_id, account_id, **kwargs):
        assert session.get(TgAccountAuthorization, primary.id).health_status == "legacy"
        primary_row = session.get(TgAccountAuthorization, primary.id)
        primary_row.health_status = "healthy"
        abc_tests._qualify_primary(session, account_id)
        calls.append(("qualify", account_id))
        return {"tenant_id": tenant_id, "account_id": account_id, "status": "qualified"}

    monkeypatch.setattr(runner, "qualify_primary_authorization", qualify)

    result = runner.run_online_abc_batch(
        db_session,
        batch_id,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
        runtime_release_sha=abc_tests.RELEASE_SHA,
        max_accounts=1,
        sleeper=lambda _: None,
    )

    assert result["chunk"]["account_ids"] == [account.id]
    assert calls == [
        ("qualify", account.id),
        ("b", account.id),
        ("c", account.id),
        ("e4", account.id),
    ]


def test_legacy_primary_probe_failure_keeps_a_and_operations_empty(
    db_session, monkeypatch,
) -> None:
    batch_id = abc_tests._apply(db_session, abc_tests._preview(db_session)["fingerprint"])["batch_id"]
    account, primary = _legacy_primary(db_session)
    before = _snapshot(account, primary)
    monkeypatch.setattr(runner, "preview_primary_qualification", abc_tests._qualification_preview)
    monkeypatch.setattr(
        runner,
        "qualify_primary_authorization",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("probe failed")),
    )
    monkeypatch.setattr(
        runner,
        "apply_abc_backup",
        lambda *_args, **_kwargs: pytest.fail("B must not start before legacy A qualification"),
    )

    result = runner.run_online_abc_batch(
        db_session,
        batch_id,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
        runtime_release_sha=abc_tests.RELEASE_SHA,
        max_accounts=1,
        sleeper=lambda _: None,
    )

    db_session.expire_all()
    account = db_session.get(TgAccount, account.id)
    primary = db_session.get(TgAccountAuthorization, primary.id)
    batch = runner._batch(db_session, batch_id)
    item = next(value for value in result["batch"]["items"] if value["account_id"] == account.id)
    model_item = runner._running_item(db_session, batch_id)
    assert item["status"] == "stopped"
    assert _snapshot(account, primary) == before
    assert model_item is None
    stopped_item = db_session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account.id,
    ))
    assert all(value is None for value in runner.online_abc_item_operations(db_session, batch, stopped_item).values())


def test_invalid_primary_never_enters_identity_probe(db_session, monkeypatch) -> None:
    batch_id = abc_tests._apply(db_session, abc_tests._preview(db_session)["fingerprint"])["batch_id"]
    account, primary = _legacy_primary(db_session)
    primary.health_status = "invalid"
    primary.last_authoritative_error_code = "authorization_key_duplicated"
    db_session.commit()
    monkeypatch.setattr(
        runner,
        "qualify_primary_authorization",
        lambda *_args, **_kwargs: pytest.fail("invalid A must stop before Telegram"),
    )

    result = runner.run_online_abc_batch(
        db_session,
        batch_id,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
        runtime_release_sha=abc_tests.RELEASE_SHA,
        max_accounts=1,
        sleeper=lambda _: None,
    )

    item = next(value for value in result["batch"]["items"] if value["account_id"] == account.id)
    assert item["blocker_code"] == "online_abc_primary_drift"


@pytest.mark.parametrize(
    ("owner", "field", "value"),
    [
        ("account", "session_ciphertext", "drifted-session"),
        ("account", "developer_app_id", 1),
        ("primary", "is_slot_current", False),
        ("primary", "provision_region_code", "my"),
        ("primary", "protected_from_cleanup", False),
    ],
)
def test_legacy_structural_drift_stops_before_probe(
    db_session, monkeypatch, owner, field, value,
) -> None:
    batch_id = abc_tests._apply(db_session, abc_tests._preview(db_session)["fingerprint"])["batch_id"]
    account, primary = _legacy_primary(db_session)
    setattr(account if owner == "account" else primary, field, value)
    db_session.commit()
    monkeypatch.setattr(
        runner,
        "qualify_primary_authorization",
        lambda *_args, **_kwargs: pytest.fail("structural drift must stop before Telegram"),
    )

    result = runner.run_online_abc_batch(
        db_session,
        batch_id,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
        runtime_release_sha=abc_tests.RELEASE_SHA,
        max_accounts=1,
        sleeper=lambda _: None,
    )

    item = next(value for value in result["batch"]["items"] if value["account_id"] == account.id)
    assert item["blocker_code"] == "online_abc_primary_drift"


def _legacy_primary(session):
    account = session.get(TgAccount, abc_tests.ACCOUNT_IDS[0])
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    primary.health_status = "legacy"
    session.commit()
    return account, primary


def _snapshot(account, primary) -> tuple:
    return (
        account.status,
        account.current_authorization_id,
        account.session_ciphertext,
        account.developer_app_id,
        account.authorization_generation,
        account.authorization_fact_generation,
        account.connection_generation,
        primary.session_ciphertext,
        primary.status,
        primary.health_status,
        primary.fact_version,
        primary.last_authoritative_error_code,
        primary.disabled_at,
    )
