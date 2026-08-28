from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models import TgAuthorizationOnlineAbcBatch, TgAuthorizationOnlineAbcItem
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.online_abc_manifest import (
    apply_post_login_online_abc_batch,
    preview_post_login_online_abc_batch,
)
from app.services.authorization_dr.online_abc_post_login import (
    run_post_login_exact_once,
)
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


def test_post_login_supervisor_runs_approved_exact_batch(db_session, monkeypatch) -> None:
    batch_id = _post_login_batch(db_session)
    from app.services.authorization_dr import online_abc_post_login as post_login

    def fake_run(session, selected_batch_id: str, **kwargs):
        assert selected_batch_id == batch_id
        assert kwargs["requested_by"] == "requester"
        assert kwargs["approved_by"] == "approver"
        assert kwargs["approval_ref"] == "POST-LOGIN-ABC"
        batch = session.get(TgAuthorizationOnlineAbcBatch, selected_batch_id)
        batch.status = "completed"
        session.commit()
        return {"batch": {"status": "completed"}}

    monkeypatch.setattr(post_login, "run_online_abc_batch", fake_run)

    result = run_post_login_exact_once(
        db_session,
        runtime_release_sha=abc_tests.RELEASE_SHA,
        poll_seconds=0.01,
    )

    assert result["status"] == "processed"
    assert result["batch_id"] == batch_id
    assert result["result"]["batch"]["status"] == "completed"


def test_post_login_supervisor_reclaims_running_batch(db_session, monkeypatch) -> None:
    batch_id = _post_login_batch(db_session)
    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = _item(db_session, batch_id)
    batch.status = "running"
    item.status = item.outcome = "running"
    db_session.commit()
    from app.services.authorization_dr import online_abc_post_login as post_login

    monkeypatch.setattr(
        post_login,
        "run_online_abc_batch",
        lambda *_args, **_kwargs: {"batch": {"status": "stopped"}},
    )

    result = run_post_login_exact_once(
        db_session,
        runtime_release_sha=abc_tests.RELEASE_SHA,
        poll_seconds=0.01,
    )

    assert result["status"] == "processed"
    assert result["batch_id"] == batch_id


def test_post_login_apply_does_not_commit_before_request_approval(db_session, monkeypatch) -> None:
    preview = preview_post_login_online_abc_batch(
        db_session,
        1,
        abc_tests.ACCOUNT_IDS[0],
        idempotency_key="post-login-atomic-approval",
        deployed_release_sha=abc_tests.RELEASE_SHA,
    )

    monkeypatch.setattr(
        db_session,
        "commit",
        lambda: (_ for _ in ()).throw(AssertionError("apply committed early")),
    )
    result = apply_post_login_online_abc_batch(
        db_session,
        1,
        abc_tests.ACCOUNT_IDS[0],
        idempotency_key="post-login-atomic-approval",
        deployed_release_sha=abc_tests.RELEASE_SHA,
        expected_fingerprint=preview["fingerprint"],
        requested_by="requester",
        approved_by="approver",
        approval_ref="POST-LOGIN-ATOMIC",
    )

    assert result["batch_id"]


def test_post_login_apply_renders_pending_slots_with_autoflush_disabled(db_session) -> None:
    db_session.autoflush = False
    preview = preview_post_login_online_abc_batch(
        db_session,
        1,
        abc_tests.ACCOUNT_IDS[0],
        idempotency_key="post-login-autoflush-disabled",
        deployed_release_sha=abc_tests.RELEASE_SHA,
    )

    result = apply_post_login_online_abc_batch(
        db_session,
        1,
        abc_tests.ACCOUNT_IDS[0],
        idempotency_key="post-login-autoflush-disabled",
        deployed_release_sha=abc_tests.RELEASE_SHA,
        expected_fingerprint=preview["fingerprint"],
        requested_by="requester",
        approved_by="approver",
        approval_ref="POST-LOGIN-AUTOFLUSH-DISABLED",
    )

    assert result["standby_1_outcome_counts"] == {"pending": 1}
    assert result["standby_2_outcome_counts"] == {"pending": 1}
    assert result["conservation"]["valid"] is True


def test_post_login_idempotent_apply_rejects_changed_frozen_manifest(db_session) -> None:
    _post_login_batch(db_session)
    changed_release = "b" * 40
    changed = preview_post_login_online_abc_batch(
        db_session,
        1,
        abc_tests.ACCOUNT_IDS[0],
        idempotency_key="post-login-exact-test",
        deployed_release_sha=changed_release,
    )

    with pytest.raises(AuthorizationDrError, match="manifest changed"):
        apply_post_login_online_abc_batch(
            db_session,
            1,
            abc_tests.ACCOUNT_IDS[0],
            idempotency_key="post-login-exact-test",
            deployed_release_sha=changed_release,
            expected_fingerprint=changed["fingerprint"],
            requested_by="requester",
            approved_by="approver",
            approval_ref="POST-LOGIN-ABC",
        )


def test_post_login_contract_error_stops_without_remote_replay(db_session) -> None:
    batch_id = _post_login_batch(db_session)

    result = run_post_login_exact_once(
        db_session,
        runtime_release_sha="b" * 40,
        poll_seconds=0.01,
    )

    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = _item(db_session, batch_id)
    assert result == {
        "status": "blocked",
        "batch_id": batch_id,
        "blocker": "runtime_image_mismatch",
    }
    assert batch.status == "stopped"
    assert item.status == "stopped"
    assert item.outcome == "runner_blocked"
    assert item.blocker_code == "runtime_image_mismatch"


def test_post_login_unexpected_error_persists_exact_batch_blocker(db_session, monkeypatch) -> None:
    batch_id = _post_login_batch(db_session)
    from app.services.authorization_dr import online_abc_post_login as post_login

    monkeypatch.setattr(
        post_login,
        "run_online_abc_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("runner exploded")),
    )

    result = run_post_login_exact_once(
        db_session,
        runtime_release_sha=abc_tests.RELEASE_SHA,
        poll_seconds=0.01,
    )

    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = _item(db_session, batch_id)
    assert result["status"] == "blocked"
    assert result["blocker"] == "post_login_runner_RuntimeError"
    assert batch.status == "stopped"
    assert item.status == "stopped"
    assert item.blocker_code == "post_login_runner_RuntimeError"


def test_supervisor_runs_post_login_lane_after_other_lanes_idle(monkeypatch) -> None:
    from scripts import authorization_online_abc_supervisor as supervisor

    monkeypatch.setattr(supervisor, "SessionLocal", _SessionContext)
    monkeypatch.setattr(supervisor, "run_online_abc_sweep_once", lambda *_args, **_kwargs: {"status": "idle"})
    monkeypatch.setattr(supervisor, "run_deferred_recovery_once", lambda *_args, **_kwargs: {"status": "idle"})
    monkeypatch.setattr(
        supervisor,
        "run_post_login_exact_once",
        lambda *_args, **_kwargs: {"status": "processed", "batch_id": "post-login"},
    )

    result = supervisor._run_once(SimpleNamespace(poll_seconds=0.01))

    assert result["lane"] == "post_login_exact"
    assert result["result"]["batch_id"] == "post-login"


def _post_login_batch(session) -> str:
    preview = preview_post_login_online_abc_batch(
        session,
        1,
        abc_tests.ACCOUNT_IDS[0],
        idempotency_key="post-login-exact-test",
        deployed_release_sha=abc_tests.RELEASE_SHA,
    )
    result = apply_post_login_online_abc_batch(
        session,
        1,
        abc_tests.ACCOUNT_IDS[0],
        idempotency_key="post-login-exact-test",
        deployed_release_sha=abc_tests.RELEASE_SHA,
        expected_fingerprint=preview["fingerprint"],
        requested_by="requester",
        approved_by="approver",
        approval_ref="POST-LOGIN-ABC",
    )
    session.commit()
    return result["batch_id"]


def _item(session, batch_id: str) -> TgAuthorizationOnlineAbcItem:
    return session.query(TgAuthorizationOnlineAbcItem).filter_by(batch_id=batch_id).one()


class _SessionContext:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False
