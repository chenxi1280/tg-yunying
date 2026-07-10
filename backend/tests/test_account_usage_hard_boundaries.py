from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountPool, AccountStatus, Action, MessageTask, Task, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.schemas.campaigns import CampaignRecommendAccountsRequest
from app.services.account_online_projection import _fallback_configured_account_ids
from app.services.campaigns import recommend_campaign_accounts, validate_selected_accounts_by_group
from app.services.group_listeners import collect_group_context, validate_listener_accounts
from app.services.messages import _resolve_send_account, choose_account
from app.services.operations_center import switch_listener_account
from app.services.task_center.account_pool import select_task_accounts
from app.services.task_center.channel_membership import candidate_accounts_for_config
from app.services.task_center.dispatcher import _apply_claim_account_policy
from app.services.task_center.executors.search_rank_deboost_planner import _rank_deboost_pool_accounts
from app.services.task_center.membership_admission import _snapshot_account_ids
from app.services.task_center.precheck import _precheck_candidate_accounts


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=1, name="tenant-1"))
        db.add_all(
            [
                AccountPool(id=1, tenant_id=1, name="normal", pool_purpose="normal"),
                AccountPool(id=2, tenant_id=1, name="rank", pool_purpose="rank_deboost"),
                AccountPool(id=3, tenant_id=1, name="receiver", pool_purpose="code_receiver"),
                AccountPool(id=4, tenant_id=1, name="disabled-rank", pool_purpose="rank_deboost", is_enabled=False),
            ]
        )
        db.commit()
        yield db


def _account(account_id: int, pool_id: int, identity: str, *, session_ciphertext: str = "session") -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=1,
        pool_id=pool_id,
        display_name=f"account-{account_id}",
        phone_masked=str(account_id),
        status=AccountStatus.ACTIVE.value,
        account_identity=identity,
        session_ciphertext=session_ciphertext,
        health_score=90,
    )


def _seed_accounts(session: Session) -> None:
    session.add_all(
        [
            _account(10, 1, "normal"),
            _account(20, 2, "normal"),
            _account(21, 2, "rank_deboost"),
            _account(30, 3, "code_receiver"),
        ]
    )
    session.commit()


@pytest.mark.parametrize(
    ("config", "expected_ids"),
    [
        ({"selection_mode": "all", "max_concurrent": 10}, [10]),
        ({"selection_mode": "manual", "account_ids": [10, 20, 21, 30], "max_concurrent": 10}, [10]),
        ({"selection_mode": "group", "account_group_id": 2, "max_concurrent": 10}, []),
    ],
)
def test_ordinary_task_selector_fail_closed_on_dedicated_or_mismatched_pool(
    session: Session,
    config: dict,
    expected_ids: list[int],
) -> None:
    _seed_accounts(session)

    accounts = select_task_accounts(session, 1, config, limit=10, enforce_capacity=False)

    assert [account.id for account in accounts] == expected_ids


def test_membership_and_precheck_candidates_use_operational_policy(session: Session) -> None:
    _seed_accounts(session)
    config = {"selection_mode": "manual", "account_ids": [10, 20, 21, 30], "max_concurrent": 10}
    task = Task(
        id="membership-snapshot",
        tenant_id=1,
        name="membership",
        type="group_join",
        type_config={"account_group_ids": [1, 2, 3], "target_operation_target_id": 1},
    )
    session.add(task)
    session.commit()

    membership_ids = [account.id for account in candidate_accounts_for_config(session, 1, config)]
    precheck_ids = [account.id for account in _precheck_candidate_accounts(session, 1, config)]

    assert membership_ids == [10]
    assert precheck_ids == [10]
    assert _snapshot_account_ids(session, task) == [10]


def test_listener_and_campaign_selected_accounts_reject_mismatched_pool(session: Session) -> None:
    _seed_accounts(session)
    group = TgGroup(id=100, tenant_id=1, tg_peer_id="-100100", title="target", auth_status="已授权运营")
    session.add(group)
    session.add(TgGroupAccount(tenant_id=1, group_id=100, account_id=20, can_send=True))
    session.commit()

    with pytest.raises(ValueError, match="account_action_not_allowed|account_purpose_mismatch"):
        validate_listener_accounts(session, group, [20])
    with pytest.raises(ValueError, match="account_action_not_allowed|account_purpose_mismatch"):
        validate_selected_accounts_by_group(session, 1, [100], {"100": [20]})


def test_campaign_recommendations_mark_mismatched_accounts_unselectable(session: Session) -> None:
    _seed_accounts(session)
    group = TgGroup(id=101, tenant_id=1, tg_peer_id="-100101", title="target", auth_status="已授权运营")
    session.add(group)
    session.add_all(
        [
            TgGroupAccount(tenant_id=1, group_id=101, account_id=10, can_send=True),
            TgGroupAccount(tenant_id=1, group_id=101, account_id=20, can_send=True),
        ]
    )
    session.commit()

    rows = recommend_campaign_accounts(session, CampaignRecommendAccountsRequest(target_group_ids=[101]), 1)
    by_account = {row["account_id"]: row for row in rows}

    assert by_account[10]["is_selectable"] is True
    assert by_account[20]["is_selectable"] is False
    assert "账号用途" in by_account[20]["unavailable_reason"]


def test_message_send_resolution_rejects_mismatched_pool(session: Session) -> None:
    _seed_accounts(session)

    with pytest.raises(ValueError, match="account_action_not_allowed|account_purpose_mismatch"):
        _resolve_send_account(session, 20, 1)


def test_message_choose_account_rejects_fixed_mismatch_and_skips_auto_mismatch(session: Session) -> None:
    _seed_accounts(session)
    group = TgGroup(id=102, tenant_id=1, tg_peer_id="-100102", title="target", auth_status="已授权运营")
    session.add(group)
    session.add_all(
        [
            TgGroupAccount(tenant_id=1, group_id=102, account_id=10, can_send=True),
            TgGroupAccount(tenant_id=1, group_id=102, account_id=20, can_send=True),
        ]
    )
    fixed = MessageTask(
        id=1,
        tenant_id=1,
        group_id=102,
        account_id=20,
        content="hi",
        target_type="group",
        idempotency_key="fixed-mismatch",
    )
    auto = MessageTask(
        id=2,
        tenant_id=1,
        group_id=102,
        content="hi",
        target_type="group",
        idempotency_key="auto-normal",
    )
    session.add_all([fixed, auto])
    session.commit()

    fixed_account, fixed_failure, _ = choose_account(session, fixed)
    auto_account, auto_failure, _ = choose_account(session, auto)

    assert fixed_account is None
    assert fixed_failure == "账号不可用"
    assert auto_account.id == 10
    assert auto_failure is None


def test_dispatcher_claim_policy_rejects_mismatched_pool(session: Session) -> None:
    _seed_accounts(session)
    task = Task(id="task-claim", tenant_id=1, name="claim", type="group_ai_chat")
    action = Action(
        id="action-claim",
        tenant_id=1,
        task_id=task.id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=20,
        status="claiming",
    )
    session.add_all([task, action])
    session.commit()

    assert _apply_claim_account_policy(session, action) is False
    assert action.status == "failed"
    assert action.result["validation_stage"] == "account_usage"


def test_online_projection_fallback_excludes_dedicated_and_mismatched_accounts(session: Session) -> None:
    _seed_accounts(session)
    task = Task(
        id="task-1",
        tenant_id=1,
        name="online",
        type="group_ai_chat",
        account_config={"selection_mode": "all"},
    )
    session.add(task)
    session.commit()

    assert _fallback_configured_account_ids(session, task, set()) == {10}


def test_online_projection_manual_fallback_excludes_dedicated_and_mismatched_accounts(session: Session) -> None:
    _seed_accounts(session)
    task = Task(
        id="task-manual-online",
        tenant_id=1,
        name="online",
        type="group_ai_chat",
        account_config={"selection_mode": "manual", "account_ids": [10, 20, 21, 30]},
    )
    session.add(task)
    session.commit()

    assert _fallback_configured_account_ids(session, task, set()) == {10}


def test_collect_group_context_skips_misconfigured_dedicated_listener(session: Session, monkeypatch) -> None:
    _seed_accounts(session)
    group = TgGroup(id=103, tenant_id=1, tg_peer_id="-100103", title="listener", auth_status="已授权运营")
    session.add(group)
    session.add(TgGroupAccount(tenant_id=1, group_id=103, account_id=21, can_send=True, is_listener=True))
    session.commit()
    called_ids: list[int] = []

    def fake_fetch(account_id, *_args, **_kwargs):
        called_ids.append(account_id)
        return []

    monkeypatch.setattr("app.services.group_listeners.credentials_for_account", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("app.services.group_listeners.gateway.fetch_group_messages", fake_fetch)

    assert collect_group_context(session, group) == 0
    assert called_ids == []


def test_switch_group_listener_rejects_dedicated_backup_account(session: Session) -> None:
    _seed_accounts(session)
    group = TgGroup(id=104, tenant_id=1, tg_peer_id="-100104", title="listener", auth_status="已授权运营", listener_enabled=True)
    session.add(group)
    offline = _account(11, 1, "normal")
    offline.status = "离线"
    session.add(offline)
    session.add_all(
        [
            TgGroupAccount(tenant_id=1, group_id=104, account_id=11, can_send=True, is_listener=True),
            TgGroupAccount(tenant_id=1, group_id=104, account_id=21, can_send=True, is_listener=False),
            Task(id="relay-listener", tenant_id=1, name="relay", type="group_relay", status="running", type_config={"source_groups": [{"group_id": 104, "is_active": True}]}),
        ]
    )
    session.commit()

    with pytest.raises(ValueError, match="备用监听账号不可用"):
        switch_listener_account(session, 1, "group", 104, 21, "tester")


def test_rank_deboost_planner_requires_enabled_matching_rank_pool(session: Session) -> None:
    session.add_all(
        [
            _account(40, 4, "rank_deboost"),
            _account(41, 2, "rank_deboost"),
            _account(42, 2, "normal"),
        ]
    )
    session.commit()

    assert [account.id for account in _rank_deboost_pool_accounts(session, 1, 2)] == [41]
    assert _rank_deboost_pool_accounts(session, 1, 4) == []
