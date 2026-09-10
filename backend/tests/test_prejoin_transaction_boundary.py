from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import OperationResult
from app.models import AccountGroupAdmissionFact, Action, Task, Tenant, TgAccount, TgGroup
from app.services.task_center import dispatcher, task_prejoin_channels as prejoin

pytestmark = pytest.mark.no_postgres


@pytest.fixture
def context(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'prejoin.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task = Task(id="task", tenant_id=1, name="t", type="group_ai_chat",
                    status="running", fulfillment_contract_version="fact_first_v3",
                    group_ai_prejoin_channel_ids=["channel_a", "channel_b"])
        account = TgAccount(id=11, tenant_id=1, display_name="a", phone_masked="11",
                            status="active", session_ciphertext="test-session")
        group = TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="g")
        action = Action(id="action", tenant_id=1, task_id="task", task_type=task.type,
                        action_type="send_message", account_id=11, status="executing",
                        claim_owner="worker", claim_token="claim", result={})
        session.add_all([Tenant(id=1, name="t"), task, account, group, action])
        session.commit()
        yield SimpleNamespace(session=session, engine=engine, task=task,
                              account=account, group=group, action=action)
    engine.dispose()


def _ensure(ctx):
    return prejoin.ensure_prejoin_channels(ctx.session, task=ctx.task,
        action=ctx.action, account=ctx.account, credentials=object(), target_group=ctx.group)


def test_remote_calls_have_no_parent_transaction_and_reuse_partial_success(context, monkeypatch):
    calls = []

    def follow(account_id, ref, ciphertext, credentials, *, invite_link):
        assert not context.session.in_transaction()
        assert (account_id, ciphertext, invite_link) == (11, "test-session", ref)
        calls.append(ref)
        return OperationResult(ref == "channel_a", detail="observed")

    monkeypatch.setattr(prejoin.gateway, "ensure_channel_membership", follow)
    assert not _ensure(context)
    context.session.commit()
    assert not _ensure(context)
    assert calls.count("channel_a") == 1
    assert calls.count("channel_b") == 2


@pytest.mark.parametrize("change", ["claim", "status", "task", "account", "configuration"])
def test_changed_dispatch_keeps_facts_without_overwriting_action(context, monkeypatch, change):
    def remote(snapshot, credentials, refs):
        assert not context.session.in_transaction()
        with Session(context.engine) as other:
            action = other.get(Action, "action")
            action.result = {"new_owner_result": True}
            if change == "claim":
                action.claim_token = "replacement"
            if change == "status":
                action.status = "cancelled"
            if change == "task":
                other.get(Task, "task").status = "paused"
            if change == "account":
                other.get(TgAccount, 11).telegram_frozen = True
            if change == "configuration":
                other.get(Task, "task").group_ai_prejoin_channel_ids = ["channel_c"]
            other.commit()
        return {ref: OperationResult(True, detail="joined") for ref in refs}

    monkeypatch.setattr(prejoin, "_follow_parallel", remote)
    with pytest.raises(prejoin.PrejoinOwnershipChanged):
        _ensure(context)
    with Session(context.engine) as other:
        assert other.get(Action, "action").result == {"new_owner_result": True}
        assert len(list(other.scalars(select(AccountGroupAdmissionFact)))) == 2


def test_dispatch_does_not_finalize_changed_ownership(context, monkeypatch):
    monkeypatch.setattr(dispatcher, "_fulfillment_route_allows_gateway", lambda *args: True)
    monkeypatch.setattr(dispatcher, "_legacy_content_scope_takeover_pending", lambda *args: False)

    def changed(*args, **kwargs):
        raise prejoin.PrejoinOwnershipChanged("changed")

    def forbidden(*args, **kwargs):
        pytest.fail("stale dispatch must not finalize the current owner's action")

    monkeypatch.setattr(dispatcher, "_dispatch_action", changed)
    monkeypatch.setattr(dispatcher, "_finalize_dispatch_action", forbidden)
    monkeypatch.setattr(dispatcher, "_release_runtime_resources", forbidden)
    assert not dispatcher.dispatch_action(context.session, context.action)


def test_remote_exception_is_not_success(context, monkeypatch):
    def failed(*args):
        assert not context.session.in_transaction()
        raise RuntimeError("transport failed")

    monkeypatch.setattr(prejoin, "_follow_parallel", failed)
    with pytest.raises(RuntimeError, match="transport failed"):
        _ensure(context)
    assert not context.session.in_transaction()


def test_current_result_fields_survive_refresh(context, monkeypatch):
    def remote(snapshot, credentials, refs):
        with Session(context.engine) as other:
            other.get(Action, "action").result = {"concurrent_field": True}
            other.commit()
        return {ref: OperationResult(True, detail="joined") for ref in refs}

    monkeypatch.setattr(prejoin, "_follow_parallel", remote)
    assert _ensure(context)
    assert context.action.result["concurrent_field"] is True


def test_no_missing_channels_keeps_callers_transaction(context, monkeypatch):
    context.task.group_ai_prejoin_channel_ids = []

    def forbidden(*args, **kwargs):
        pytest.fail("nothing to follow must not commit or invoke Telegram")

    monkeypatch.setattr(context.session, "commit", forbidden)
    monkeypatch.setattr(prejoin, "_follow_parallel", forbidden)
    assert _ensure(context)


def test_ownership_signal_is_not_converted_to_dispatch_failure(context, monkeypatch):
    monkeypatch.setattr(dispatcher, "_awaiting_legacy_review", lambda *args: False)
    monkeypatch.setattr(dispatcher, "_action_pre_dispatch_handled", lambda *args: False)
    monkeypatch.setattr(dispatcher, "_dispatch_account", lambda *args: context.account)
    monkeypatch.setattr(dispatcher, "_latest_execution_attempt", lambda *args: None)
    monkeypatch.setattr(dispatcher, "validate_action_payload", lambda *args: object())

    def changed(*args, **kwargs):
        raise prejoin.PrejoinOwnershipChanged("changed")

    monkeypatch.setattr(dispatcher, "_dispatch_validated_action", changed)
    with pytest.raises(prejoin.PrejoinOwnershipChanged):
        dispatcher._dispatch_action(context.session, context.action,
                                    generation_dependencies=None, comment_generation_dependencies=None)
