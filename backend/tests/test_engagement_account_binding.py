from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountGroupMembershipSnapshotSet,
    AccountBehaviorBudgetPolicyRevision,
    AccountPool,
    AccountPoolConcurrencyPolicyRevision,
    ExecutionResiliencePolicyRevision,
    OperationTarget,
    TaskAccountGroupBindingSetRevision,
    Tenant,
    TgAccount,
)
from app.schemas import ChannelLikeTaskCreate
from app.services.task_center.account_pool import select_task_accounts
from app.services.task_center.engagement_binding import (
    activate_due_binding,
    freeze_membership_snapshot,
    synchronize_task_binding,
    validate_engagement_binding,
)
from app.services.task_center.service import create_channel_like_task


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(session: Session) -> None:
    session.add(Tenant(id=1, name="默认运营空间"))
    _seed_pools(session)
    _seed_accounts(session)
    session.add(
        OperationTarget(
            id=101,
            tenant_id=1,
            target_type="channel",
            tg_peer_id="-100101",
            title="测试频道",
        )
    )
    session.commit()


def _seed_pools(session: Session) -> None:
    session.add_all(
        [
            AccountPool(id=1, tenant_id=1, name="互动一组"),
            AccountPool(id=2, tenant_id=1, name="互动二组"),
            AccountPool(
                id=3,
                tenant_id=1,
                name="接码专用",
                pool_purpose="code_receiver",
                system_key="code_receiver",
                is_system=True,
            ),
        ]
    )


def _seed_accounts(session: Session) -> None:
    session.add_all(
        [
            TgAccount(
                id=11,
                tenant_id=1,
                pool_id=1,
                display_name="账号11",
                phone_masked="11",
                status="在线",
            ),
            TgAccount(
                id=21,
                tenant_id=1,
                pool_id=2,
                display_name="账号21",
                phone_masked="21",
                status="在线",
            ),
            TgAccount(
                id=31,
                tenant_id=1,
                pool_id=3,
                display_name="账号31",
                phone_masked="31",
                status="在线",
                account_identity="code_receiver",
            ),
        ]
    )


def _payload(**overrides) -> ChannelLikeTaskCreate:
    data = {
        "name": "统一点赞",
        "target_channel_id": 101,
        "engagement_contract_version": "unified_engagement_v1",
        "account_group_ids": [2, 1],
        "concurrency_limit_per_group": 4,
        "daily_reaction_cap": 80,
    }
    data.update(overrides)
    return ChannelLikeTaskCreate(**data)


def test_create_engagement_task_freezes_canonical_group_binding() -> None:
    with _session() as session:
        _seed(session)

        task = create_channel_like_task(session, 1, _payload(), "tester")
        binding = session.scalar(
            select(TaskAccountGroupBindingSetRevision).where(
                TaskAccountGroupBindingSetRevision.task_id == task.id
            )
        )

        assert task.type_config["account_group_ids"] == [1, 2]
        assert task.account_config["selection_mode"] == "group"
        assert task.account_config["account_group_ids"] == [1, 2]
        assert task.account_config["max_concurrent"] == 4
        assert binding is not None
        assert binding.account_group_ids == [1, 2]
        assert len(binding.binding_set_hash) == 64
        assert session.scalar(select(ExecutionResiliencePolicyRevision)) is not None
        assert session.scalar(select(AccountBehaviorBudgetPolicyRevision)) is not None
        pool_policies = list(session.scalars(select(AccountPoolConcurrencyPolicyRevision)))
        assert [row.account_pool_id for row in pool_policies] == [1, 2]


def test_task_group_concurrency_cannot_exceed_pool_physical_limit() -> None:
    with _session() as session:
        _seed(session)

        with pytest.raises(
            ValueError,
            match="task_group_concurrency_exceeds_pool_limit:1:6>5",
        ):
            create_channel_like_task(
                session,
                1,
                _payload(concurrency_limit_per_group=6),
                "tester",
            )


def test_unified_task_rejects_non_beijing_task_day() -> None:
    with _session() as session:
        _seed(session)

        with pytest.raises(
            ValueError,
            match="unified_engagement_timezone_must_be_Asia/Shanghai",
        ):
            create_channel_like_task(
                session,
                1,
                _payload(timezone="UTC"),
                "tester",
            )


def test_membership_is_frozen_per_participation_unit_not_in_binding() -> None:
    with _session() as session:
        _seed(session)
        task = create_channel_like_task(session, 1, _payload(), "tester")
        binding = session.scalar(
            select(TaskAccountGroupBindingSetRevision).where(
                TaskAccountGroupBindingSetRevision.task_id == task.id
            )
        )
        assert binding is not None
        binding_hash = binding.binding_set_hash
        first = freeze_membership_snapshot(session, task, participation_unit="2026-09-04")
        session.add(
            TgAccount(
                id=22,
                tenant_id=1,
                pool_id=2,
                display_name="账号22",
                phone_masked="22",
                status="在线",
            )
        )
        session.flush()
        second = freeze_membership_snapshot(session, task, participation_unit="2026-09-05")

        assert isinstance(first, AccountGroupMembershipSnapshotSet)
        assert first.member_account_ids == [11, 21]
        assert second.member_account_ids == [11, 21, 22]
        assert first.member_union_hash != second.member_union_hash
        assert binding.binding_set_hash == binding_hash


def test_running_binding_change_activates_only_at_next_task_day() -> None:
    with _session() as session:
        _seed(session)
        task = create_channel_like_task(session, 1, _payload(), "tester")
        task.status = "running"
        task.type_config = {
            **task.type_config,
            "account_group_ids": [1],
            "concurrency_limit_per_group": 2,
        }

        scheduled = synchronize_task_binding(session, task)

        assert scheduled is not None
        assert scheduled.state == "scheduled"
        assert task.type_config["account_group_ids"] == [1, 2]
        assert task.account_config["account_group_ids"] == [1, 2]
        activated = activate_due_binding(
            session,
            task,
            period_start=scheduled.effective_from,
        )
        assert activated is scheduled
        assert scheduled.state == "active"
        assert task.type_config["account_group_ids"] == [1]
        assert task.account_config["account_group_ids"] == [1]


def test_multi_group_selector_uses_union_without_special_pool() -> None:
    with _session() as session:
        _seed(session)

        accounts = select_task_accounts(
            session,
            1,
            {
                "selection_mode": "group",
                "account_group_ids": [1, 2],
                "max_concurrent": 10,
                "cooldown_per_account_minutes": 0,
            },
            enforce_capacity=False,
        )

        assert [account.id for account in accounts] == [11, 21]


def test_specialized_or_cross_tenant_group_is_rejected() -> None:
    with _session() as session:
        _seed(session)

        for group_ids, expected in [
            ([3], "account_group_purpose_mismatch:3"),
            ([999], "account_group_not_found_or_cross_tenant"),
        ]:
            try:
                validate_engagement_binding(
                    session,
                    1,
                    "channel_like",
                    {
                        "engagement_contract_version": "unified_engagement_v1",
                        "account_selection_mode": "group",
                        "account_group_ids": group_ids,
                        "concurrency_limit_per_group": 5,
                    },
                )
            except ValueError as exc:
                assert str(exc) == expected
            else:
                raise AssertionError("invalid account group was accepted")


def test_missing_or_duplicate_group_binding_is_rejected() -> None:
    with _session() as session:
        _seed(session)

        for config, expected in [
            (
                {"engagement_contract_version": "unified_engagement_v1", "account_selection_mode": "group", "account_group_ids": []},
                "account_group_ids 至少选择一个账号分组",
            ),
            (
                {"engagement_contract_version": "unified_engagement_v1", "account_selection_mode": "group", "account_group_ids": [1, 1]},
                "account_group_ids 不得重复",
            ),
        ]:
            try:
                validate_engagement_binding(session, 1, "channel_view", config)
            except ValueError as exc:
                assert str(exc) == expected
            else:
                raise AssertionError("invalid binding was accepted")
