import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from telethon import types

from app.database import Base
from app.config import Settings
from app.integrations.telegram.account_freeze import read_account_freeze_health
from app.integrations.telegram.contracts import AccountHealth, OperationResult
from app.integrations.telegram.gateway import TelethonTelegramGateway
from app.models import AccountStatus, Action, ExecutionAttempt, TgAccount, TgAccountOnlineState
from app.services.account_freeze import AccountFrozenBeforeGateway, apply_freeze_observation, guard_account_call_start
from app.services.account_online_probe import OnlineProbeResult, _apply_probe_result
from app.services.account_usage_policy import apply_operational_account_filters, assert_account_action_allowed
from app.services.task_center import dispatcher

pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 8, 4, tzinfo=timezone.utc)
FROZEN_DETAIL = "The method is not available for frozen accounts (caused by JoinChannelRequest)"


@pytest.fixture(scope="module")
def engine():
    database = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


@pytest.fixture
def session(engine):
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(connection) as current:
            current.add(TgAccount(id=1, tenant_id=1, display_name="test", phone_masked="test", status="在线"))
            current.add(TgAccountOnlineState(id="state", tenant_id=1, account_id=1, online_status="online"))
            current.flush()
            yield current
        transaction.rollback()


@pytest.mark.parametrize("message", [FROZEN_DETAIL, "FROZEN_METHOD_INVALID", "FROZEN_PARTICIPANT_MISSING", "FrozenMethodInvalidError"])
def test_freeze_error_precedes_target_mapping(message):
    result = TelethonTelegramGateway._map_send_error(RuntimeError(message))
    assert result.failure_type == "账号不可用"
    assert result.detail == message


def test_group_permission_does_not_freeze_account():
    result = TelethonTelegramGateway._map_send_error(RuntimeError(
        "The channel specified is private and you lack permission to access it (caused by SendMessageRequest)"))
    assert result.failure_type == "群无权限"


def test_target_permission_probe_preserves_account_freeze():
    class Client:
        async def get_permissions(self, target, account):
            raise RuntimeError(FROZEN_DETAIL)

    gateway = TelethonTelegramGateway(Settings())
    result = asyncio.run(gateway._probe_resolved_target_permissions(
        Client(), object(), target_peer_id="-1001", target_type="group", require_send=True))
    assert result.failure_type == "账号不可用"
    assert result.detail == FROZEN_DETAIL


def test_readable_profile_with_frozen_config_is_not_healthy(monkeypatch):
    from app.integrations.telegram import DeveloperAppCredentials

    calls = []

    class Client:
        async def connect(self):
            calls.append("connect")

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            calls.append("profile_readable")

        async def __call__(self, request):
            return types.help.AppConfig(1, types.JsonObject([
                types.JsonObjectValue("freeze_since_date", types.JsonNumber(1720000000))]))

        async def disconnect(self):
            calls.append("disconnect")

    gateway = TelethonTelegramGateway(Settings())
    monkeypatch.setattr("app.integrations.telegram.gateway.decrypt_session", lambda value: "test")
    monkeypatch.setattr(gateway, "_new_client", lambda *a, **k: Client())
    health = asyncio.run(gateway._health_async("test", DeveloperAppCredentials(1, 1, "test", 1)))
    assert health.telegram_frozen and health.status == "疑似封禁"
    assert calls == ["connect", "profile_readable", "disconnect"]


@pytest.mark.parametrize("since,expected", [(None, False), (0, False), (1720000000, True)])
def test_full_config_freeze_fact(since, expected):
    class Client:
        async def __call__(self, request):
            assert request.hash == 0
            entries = [] if since is None else [types.JsonObjectValue("freeze_since_date", types.JsonNumber(since))]
            return types.help.AppConfig(hash=7, config=types.JsonObject(entries))

    health = asyncio.run(read_account_freeze_health(Client()))
    assert health.telegram_frozen is expected
    assert (health.status == "在线") is not expected
    assert health.freeze_observed_at is not None


@pytest.mark.parametrize("response", [
    types.help.AppConfigNotModified(),
    types.help.AppConfig(1, types.JsonNull()),
    types.help.AppConfig(1, types.JsonObject([types.JsonObjectValue("freeze_since_date", types.JsonString("bad"))])),
])
def test_incomplete_or_invalid_config_is_not_success(response):
    class Client:
        async def __call__(self, request):
            return response

    with pytest.raises(RuntimeError, match="telegram_freeze_check"):
        asyncio.run(read_account_freeze_health(Client()))


def _probe(session, *, frozen=None, observed_at=None, generations=None):
    account = session.get(TgAccount, 1)
    state = session.get(TgAccountOnlineState, "state")
    result = OnlineProbeResult(account_id=1, generations=generations, health=AccountHealth(
        status="在线", health_score=95, detail="health", telegram_frozen=frozen, freeze_observed_at=observed_at))
    _apply_probe_result(session, account, state, NOW, result)
    session.flush()
    return account, state


def test_plain_connectivity_cannot_clear_freeze(session):
    account = session.get(TgAccount, 1)
    apply_freeze_observation(account, frozen=True, observed_at=NOW)
    account, state = _probe(session)
    assert account.telegram_frozen and account.status == "疑似封禁"
    assert state.online_status == "blocked" and state.failure_type == "account_frozen"


def test_old_healthy_result_cannot_overwrite_new_freeze(session):
    account = session.get(TgAccount, 1)
    apply_freeze_observation(account, frozen=True, observed_at=NOW)
    account, state = _probe(session, frozen=False, observed_at=NOW - timedelta(seconds=1))
    assert account.telegram_frozen and account.status == "疑似封禁"
    assert state.online_status == "blocked"


def test_new_authoritative_unfreeze_restores_online(session):
    account = session.get(TgAccount, 1)
    apply_freeze_observation(account, frozen=True, observed_at=NOW)
    account, state = _probe(session, frozen=False, observed_at=NOW + timedelta(seconds=1))
    assert not account.telegram_frozen and account.status == "在线"
    assert state.online_status == "online"
    assert not apply_freeze_observation(account, frozen=True, observed_at=NOW)
    assert not account.telegram_frozen


def test_old_authorization_probe_cannot_clear_freeze(session):
    account = session.get(TgAccount, 1)
    apply_freeze_observation(account, frozen=True, observed_at=NOW)
    account, state = _probe(session, frozen=False, observed_at=NOW + timedelta(seconds=1), generations=(0, 0))
    assert account.telegram_frozen
    assert state.failure_type == "account_health_probe_stale_identity"


def test_online_projection_cannot_bypass_frozen_eligibility(session):
    account = session.get(TgAccount, 1)
    apply_freeze_observation(account, frozen=True, observed_at=NOW)
    account.status = "在线"
    session.flush()
    assert list(session.scalars(apply_operational_account_filters(select(TgAccount)))) == []
    with pytest.raises(ValueError, match="account_frozen"):
        assert_account_action_allowed(account, None, "operational_task")
    assert assert_account_action_allowed(account, None, "account_health_probe") == "normal"


def _attempt(session):
    action = Action(id="action", tenant_id=1, task_id="task", account_id=1,
        task_type="group_ai_chat", action_type="ensure_target_membership", status="executing", payload={}, result={})
    attempt = ExecutionAttempt(id="attempt", tenant_id=1, action_id=action.id, account_id=1,
        status="before_call", before_call_at=NOW, result_snapshot={})
    session.add_all([action, attempt])
    session.flush()
    return action, attempt


def test_current_freeze_fences_existing_pending_work(session):
    account = session.get(TgAccount, 1)
    action, attempt = _attempt(session)
    apply_freeze_observation(account, frozen=True, observed_at=NOW)
    with pytest.raises(AccountFrozenBeforeGateway):
        guard_account_call_start(session, attempt)
    assert attempt.gateway_call_started_at is None
    assert attempt.result_snapshot["remote_mutation_started"] is False


def test_failed_probe_without_freeze_observation_fences_existing_membership(session):
    from app.models import Task
    from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked

    session.add(Task(id="task", tenant_id=1, name="freeze regression", type="group_ai_chat", status="running"))
    account = session.get(TgAccount, 1)
    state = session.get(TgAccountOnlineState, "state")
    _apply_probe_result(session, account, state, NOW, OnlineProbeResult(
        account_id=1, error=ValueError("Request was unsuccessful 6 time(s)"), completed_at=NOW))
    _, attempt = _attempt(session)
    with pytest.raises(RuntimeResourceBlocked, match="account_freeze_observation_required"):
        dispatcher._mark_gateway_call_started(session, attempt, commit=False)
    assert attempt.gateway_call_started_at is None
    assert account.telegram_freeze_observed_at is None
    apply_freeze_observation(account, frozen=False, observed_at=NOW + timedelta(seconds=1))
    guard_account_call_start(session, attempt)


def test_frozen_membership_bypasses_group_rescue_and_preserves_unknown(session, monkeypatch):
    from types import SimpleNamespace

    account = session.get(TgAccount, 1)
    action, attempt = _attempt(session)
    attempt.gateway_call_started_at = NOW
    monkeypatch.setattr(dispatcher, "_recover_group_send_permission_with_linked_channel",
        lambda *a, **k: pytest.fail("Frozen accounts must not enter group rescue"))
    monkeypatch.setattr(dispatcher, "_release_runtime_resources", lambda *a, **k: None)
    monkeypatch.setattr(dispatcher, "_finalize_speaker_after_send", lambda *a, **k: None)
    monkeypatch.setattr(dispatcher, "_finish_execution_attempt", lambda *a, **k: None)
    ctx = SimpleNamespace(account=account, action=action, attempt=attempt)
    result = OperationResult(False, "失败", "群无权限", FROZEN_DETAIL)
    assert dispatcher._handle_group_send_permission_denied(ctx, result, membership_status="joined", skip_on_failure=True)
    assert account.telegram_frozen and account.status == "疑似封禁"
    assert action.status == "unknown_after_send"
    assert attempt.gateway_call_started_at == NOW
