from __future__ import annotations

import pytest
from sqlalchemy import select

import app.services.authorization_dr.online_abc_runner as runner
from app.models import (
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
)
from app.services.authorization_dr.online_abc import start_next_online_abc_item
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


def test_legacy_only_primary_bootstraps_b_before_qualification(db_session, monkeypatch) -> None:
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
        ("b", account.id),
        ("qualify", account.id),
        ("c", account.id),
        ("e4", account.id),
    ]


def test_legacy_primary_bootstrap_probe_failure_keeps_a_and_operations_empty(
    db_session, monkeypatch,
) -> None:
    batch_id = abc_tests._apply(db_session, abc_tests._preview(db_session)["fingerprint"])["batch_id"]
    account, primary = _legacy_primary(db_session)
    before = _snapshot(account, primary)
    monkeypatch.setattr(
        runner,
        "preview_abc_backup",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("probe failed")),
    )
    monkeypatch.setattr(
        runner,
        "qualify_primary_authorization",
        lambda *_args, **_kwargs: pytest.fail("qualification must not run after bootstrap probe failure"),
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


def test_legacy_primary_qualification_failure_keeps_a_and_succeeded_b_observer(
    db_session, monkeypatch,
) -> None:
    batch_id = abc_tests._apply(db_session, abc_tests._preview(db_session)["fingerprint"])["batch_id"]
    account, primary = _legacy_primary(db_session)
    before = _snapshot(account, primary)
    calls: list[tuple[str, int]] = []
    abc_tests._mock_runner_success(monkeypatch, calls)
    monkeypatch.setattr(
        runner,
        "qualify_primary_authorization",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("hash unresolved")),
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
    item = db_session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account.id,
    ))
    operations = runner.online_abc_item_operations(db_session, batch, item)
    assert result["batch"]["status"] == "stopped"
    assert item.blocker_code == "ValueError"
    assert _snapshot(account, primary) == before
    assert operations["b"].status == "succeeded"
    assert operations["c"] is None
    assert operations["e4"] is None


def test_resume_reopens_legacy_pre_primary_without_operations(db_session) -> None:
    batch_id, item, _primary = _stopped_legacy_before_primary(db_session)

    result = runner.resume_online_abc_batch(
        db_session,
        batch_id,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
        runtime_release_sha="2" * 40,
    )

    assert result["batch"]["status"] == "running"
    assert result["current_item"]["id"] == item.id
    assert result["operations"] == {"b": None, "c": None, "e4": None}


def test_resume_reuses_succeeded_b_observer_before_legacy_a_qualification(db_session) -> None:
    batch_id, item, primary = _stopped_legacy_before_primary(db_session)
    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    candidate = _bootstrap_b_candidate(db_session, item)
    operation = abc_tests._add_operation(
        db_session,
        item.account_id,
        runner.online_abc_operation_keys(batch, item)["b"],
        "succeeded",
    )
    _freeze_b_operation(operation, item=item, primary=primary, candidate=candidate)
    db_session.commit()

    result = runner.resume_online_abc_batch(
        db_session,
        batch_id,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
        runtime_release_sha="2" * 40,
    )

    assert result["batch"]["status"] == "running"
    assert result["current_item"]["id"] == item.id
    assert result["operations"]["b"]["id"] == operation.id
    assert result["operations"]["c"] is None
    assert result["operations"]["e4"] is None


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


def _stopped_legacy_before_primary(session):
    batch_id = abc_tests._apply(session, abc_tests._preview(session)["fingerprint"])["batch_id"]
    start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-10")
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = runner._context(session, batch_id)[1]
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    primary.health_status = "legacy"
    item.status = "stopped"
    item.outcome = "runner_blocked"
    item.blocker_code = "ValueError"
    batch.status = "stopped"
    session.commit()
    return batch_id, item, primary


def _bootstrap_b_candidate(session, item):
    candidate = TgAccountAuthorization(
        tenant_id=1, account_id=item.account_id, role="standby_1", logical_slot="standby_1",
        provision_region_code="sv", developer_app_id=1, session_ciphertext="bootstrap-b",
        telegram_authorization_hash_ciphertext="b-hash", telegram_user_id_digest="1" * 64,
        auth_key_fingerprint_digest="9" * 64, status="standby", health_status="healthy",
        is_current=False, is_slot_current=True, protected_from_cleanup=True,
    )
    session.add(candidate)
    session.flush()
    return candidate


def _freeze_b_operation(operation, *, item, primary, candidate) -> None:
    operation.source_authorization_id = primary.id
    operation.code_source_authorization_id = primary.id
    operation.candidate_authorization_id = candidate.id
    operation.expected_current_authorization_id = primary.id
    operation.expected_authorization_generation = item.authorization_generation
    operation.expected_authorization_fact_generation = item.authorization_fact_generation
    operation.expected_connection_generation = item.connection_generation
    operation.expected_code_source_fact_version = item.primary_fact_version
    operation.expected_code_source_user_id_digest = "1" * 64
    operation.expected_code_source_auth_key_digest = "2" * 64


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
