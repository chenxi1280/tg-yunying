from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPool,
    BotProtocolSample,
    ProxyAirportNode,
    ProxyAirportSubscription,
    Tenant,
    TgAccount,
)
from app.services.task_center.search_rank_deboost import (
    RANK_DEBOOST_GRADUATION_ACCOUNT_LIMIT,
    compute_deboost_click_targets,
    validate_rank_deboost_preconditions,
)


def _results(count: int, *, target_position: int, target_username: str = "my_target", exempt_position: int | None = None, exempt_username: str = "exempt_group") -> list[dict]:
    """构造 count 个搜索结果，按 position 升序。"""
    items: list[dict] = []
    for position in range(1, count + 1):
        if position == target_position:
            username = target_username
        elif exempt_position is not None and position == exempt_position:
            username = exempt_username
        else:
            username = f"competitor_{position}"
        items.append(
            {
                "position": position,
                "username": username,
                "peer_id": f"-100{position}",
                "title": f"群 {position}",
            }
        )
    return items


@pytest.mark.no_postgres
def test_compute_deboost_click_targets_excludes_target_exempt_and_lower_ranked() -> None:
    """我方目标群在位置 3，豁免群在位置 5，10 个结果 → 排除目标(3)、豁免(5)、排名比我方低的(4,6,7,8,9,10) → click_targets 2 个（位置 1,2），skipped_reason=None。"""
    results = _results(count=10, target_position=3, exempt_position=5)
    target_username = results[2]["username"]
    exempt_username = results[4]["username"]

    decision = compute_deboost_click_targets(
        search_results=results,
        my_target_ids=[target_username],
        exempt_group_username=exempt_username,
    )

    assert decision["my_target_position"] == 3
    assert decision["exempt_position"] == 5
    assert decision["skipped_reason"] is None
    assert len(decision["click_targets"]) == 2
    positions = sorted(item["position"] for item in decision["click_targets"])
    assert positions == [1, 2]


@pytest.mark.no_postgres
def test_compute_deboost_click_targets_returns_target_not_in_results_when_my_target_ids_empty() -> None:
    """my_target_ids 为空时无法匹配我方目标群 → skipped_reason=target_not_in_results。"""
    results = _results(count=10, target_position=3, exempt_position=5)

    decision = compute_deboost_click_targets(
        search_results=results,
        my_target_ids=[],
        exempt_group_username="exempt_group",
    )

    assert decision["click_targets"] == []
    assert decision["my_target_position"] is None
    assert decision["exempt_position"] is None
    assert decision["skipped_reason"] == "target_not_in_results"


@pytest.mark.no_postgres
def test_compute_deboost_click_targets_returns_target_not_in_results_when_target_absent() -> None:
    """我方目标群未出现在搜索结果中 → skipped_reason=target_not_in_results。"""
    results = _results(count=10, target_position=3, exempt_position=5)

    decision = compute_deboost_click_targets(
        search_results=results,
        my_target_ids=["absent_target_username"],
        exempt_group_username="exempt_group",
    )

    assert decision["click_targets"] == []
    assert decision["my_target_position"] is None
    assert decision["skipped_reason"] == "target_not_in_results"


@pytest.mark.no_postgres
def test_compute_deboost_click_targets_returns_all_exempt_clicks_when_no_click_targets() -> None:
    """所有结果都被白名单豁免（罕见） → skipped_reason=all_exempt_clicks。

    构造：搜索结果只有 1 个群，且它就是我方目标群 → click_targets=[]，skipped_reason=all_exempt_clicks。
    """
    results = [
        {
            "position": 1,
            "username": "my_target",
            "peer_id": "-1001",
            "title": "我方目标群",
        }
    ]

    decision = compute_deboost_click_targets(
        search_results=results,
        my_target_ids=["my_target"],
        exempt_group_username="exempt_group",
    )

    assert decision["my_target_position"] == 1
    assert decision["exempt_position"] is None
    assert decision["click_targets"] == []
    assert decision["skipped_reason"] == "all_exempt_clicks"


@pytest.mark.no_postgres
def test_compute_deboost_click_targets_handles_missing_exempt_group() -> None:
    """豁免群未出现在搜索结果中 → 仅排除我方目标群和排名更低的群。"""
    results = _results(count=5, target_position=3, exempt_position=None)

    decision = compute_deboost_click_targets(
        search_results=results,
        my_target_ids=["my_target"],
        exempt_group_username="absent_exempt_group",
    )

    assert decision["my_target_position"] == 3
    assert decision["exempt_position"] is None
    assert decision["skipped_reason"] is None
    assert len(decision["click_targets"]) == 2
    positions = sorted(item["position"] for item in decision["click_targets"])
    assert positions == [1, 2]


@pytest.mark.no_postgres
def test_compute_deboost_click_targets_matches_by_peer_id() -> None:
    """my_target_ids 通过 peer_id 匹配（非 username）。"""
    results = _results(count=5, target_position=3, exempt_position=5)
    target_peer_id = results[2]["peer_id"]

    decision = compute_deboost_click_targets(
        search_results=results,
        my_target_ids=[target_peer_id],
        exempt_group_username="exempt_group",
    )

    assert decision["my_target_position"] == 3
    assert decision["skipped_reason"] is None


@pytest.mark.no_postgres
def test_compute_deboost_click_targets_matches_by_int_id() -> None:
    """my_target_ids 通过 id 字段匹配。"""
    results = [
        {"position": 1, "username": "g1", "peer_id": "-1001", "id": 11},
        {"position": 2, "username": "g2", "peer_id": "-1002", "id": 22},
        {"position": 3, "username": "my_target", "peer_id": "-1003", "id": 33},
        {"position": 4, "username": "g4", "peer_id": "-1004", "id": 44},
        {"position": 5, "username": "exempt_group", "peer_id": "-1005", "id": 55},
    ]

    decision = compute_deboost_click_targets(
        search_results=results,
        my_target_ids=[33],
        exempt_group_username="exempt_group",
    )

    assert decision["my_target_position"] == 3
    assert decision["exempt_position"] == 5
    assert decision["skipped_reason"] is None
    assert len(decision["click_targets"]) == 2


# --- 灰度账号数硬上限 scenario ---


def _make_preconditions_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    session.commit()
    return session


def _seed_rank_deboost_preconditions(
    session: Session, *, account_count: int
) -> tuple[int, int]:
    """构造通过协议样本与节点校验的前置数据，返回 (account_pool_id, proxy_airport_node_id)。"""
    pool = AccountPool(tenant_id=1, name="rank_deboost_pool", pool_purpose="rank_deboost")
    session.add(pool)
    session.flush()

    subscription = ProxyAirportSubscription(tenant_id=1, name="主订阅")
    session.add(subscription)
    session.flush()
    node = ProxyAirportNode(
        tenant_id=1,
        subscription_id=subscription.id,
        node_key="node-1",
        node_name="节点1",
        status="healthy",
    )
    session.add(node)
    session.flush()

    # 协议样本达阈值（start_response×2、search_results×5、pagination_response×3、
    # button_structure 含 3 种 button_effect、exit_ip_observation×3）
    for _ in range(2):
        session.add(
            BotProtocolSample(
                tenant_id=1,
                bot_username="jisou",
                sample_type="start_response",
                sample_purpose="rank_deboost",
                is_active=True,
            )
        )
    for _ in range(5):
        session.add(
            BotProtocolSample(
                tenant_id=1,
                bot_username="jisou",
                sample_type="search_results",
                sample_purpose="rank_deboost",
                is_active=True,
            )
        )
    for _ in range(3):
        session.add(
            BotProtocolSample(
                tenant_id=1,
                bot_username="jisou",
                sample_type="pagination_response",
                sample_purpose="rank_deboost",
                is_active=True,
            )
        )
    for effect in ("navigate_only", "join_candidate", "external_http_url"):
        session.add(
            BotProtocolSample(
                tenant_id=1,
                bot_username="jisou",
                sample_type="button_structure",
                sample_purpose="rank_deboost",
                is_active=True,
                structure_json={"button_effect": effect},
            )
        )
    for _ in range(3):
        session.add(
            BotProtocolSample(
                tenant_id=1,
                bot_username="jisou",
                sample_type="exit_ip_observation",
                sample_purpose="rank_deboost",
                is_active=True,
            )
        )

    for i in range(account_count):
        session.add(
            TgAccount(
                tenant_id=1,
                pool_id=pool.id,
                display_name=f"账号{i}",
                phone_masked=f"+86138{i:08d}",
                account_identity="rank_deboost",
                status="在线",
            )
        )

    session.commit()
    return pool.id, node.id


@pytest.mark.no_postgres
def test_validate_preconditions_passes_when_account_count_at_limit() -> None:
    """灰度账号数 = 硬上限（10）时通过。"""
    session = _make_preconditions_session()
    pool_id, node_id = _seed_rank_deboost_preconditions(
        session, account_count=RANK_DEBOOST_GRADUATION_ACCOUNT_LIMIT
    )

    validate_rank_deboost_preconditions(
        session,
        tenant_id=1,
        account_pool_id=pool_id,
        proxy_airport_node_id=node_id,
        target_group_ids=[1],
    )


@pytest.mark.no_postgres
def test_validate_preconditions_rejects_when_account_count_exceeds_limit() -> None:
    """灰度账号数 > 硬上限（11）时被拒，错误消息含上限信息。"""
    session = _make_preconditions_session()
    pool_id, node_id = _seed_rank_deboost_preconditions(
        session, account_count=RANK_DEBOOST_GRADUATION_ACCOUNT_LIMIT + 1
    )

    with pytest.raises(ValueError) as exc_info:
        validate_rank_deboost_preconditions(
            session,
            tenant_id=1,
            account_pool_id=pool_id,
            proxy_airport_node_id=node_id,
            target_group_ids=[1],
        )
    msg = str(exc_info.value)
    assert "灰度账号数" in msg
    assert str(RANK_DEBOOST_GRADUATION_ACCOUNT_LIMIT) in msg
