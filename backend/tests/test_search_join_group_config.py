from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import OperationTarget, Tenant, TgAccount
from app.security import decrypt_secret
from app.schemas.task_center import SearchJoinGroupTaskCreate
from app.services.task_center.service import create_and_start_search_join_group_task, create_search_join_group_task


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id=1, name="默认运营空间"))
        db.add(
            OperationTarget(
                id=17,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-10017",
                title="上海留学交流群",
                username="shanghai_study_group",
            )
        )
        db.add(
            TgAccount(
                id=101,
                tenant_id=1,
                display_name="搜索账号",
                phone_masked="101",
                status="在线",
                session_ciphertext="session-101",
            )
        )
        db.commit()
        yield db


def _payload(**overrides) -> SearchJoinGroupTaskCreate:
    data = {
        "name": "上海搜索入群",
        "target_operation_target_id": 17,
        "search_bots": [{"username": "jisou", "display_name": "极搜"}],
        "keywords": ["上海 留学", "上海 国际学校"],
        "business_region": "CN-SH",
        "pre_join_decoy_click_min": 1,
        "pre_join_decoy_click_max": 2,
        "post_join_safe_navigation_min": 0,
        "post_join_safe_navigation_max": 1,
        "decoy_join_enabled": False,
        "hourly_min_successful_joins": 2,
    }
    data.update(overrides)
    return SearchJoinGroupTaskCreate(**data)


@pytest.mark.no_postgres
def test_search_join_group_create_persists_fixed_mode_and_keyword_hashes(session: Session) -> None:
    task = create_search_join_group_task(session, 1, _payload(), actor="tester")

    assert task.type == "search_join_group"
    assert task.type_config["execution_mode"] == "mtproto_userbot"
    assert task.type_config["target_operation_target_id"] == 17
    keyword_hashes = task.type_config["keyword_hashes"]
    keyword_ciphertexts = task.type_config["keyword_text_ciphertexts"]
    assert len(keyword_hashes) == 2
    assert [decrypt_secret(item) for item in keyword_ciphertexts] == ["上海 留学", "上海 国际学校"]
    assert all(len(item) == 64 for item in keyword_hashes)
    assert "上海 留学" not in str(task.type_config)
    assert "上海 国际学校" not in str(task.type_config)


@pytest.mark.no_postgres
def test_search_join_group_rejects_unsafe_non_target_navigation() -> None:
    with pytest.raises(ValidationError, match="非目标安全浏览总量不能超过 3"):
        _payload(pre_join_decoy_click_min=2, pre_join_decoy_click_max=3, post_join_safe_navigation_min=1, post_join_safe_navigation_max=1)

    with pytest.raises(ValidationError, match="不得加入非目标群"):
        _payload(decoy_join_enabled=True)


@pytest.mark.no_postgres
def test_search_join_group_requires_keyword_hash_material() -> None:
    with pytest.raises(ValidationError, match="keywords 或 keyword_hashes 至少提供一个"):
        _payload(keywords=[], keyword_hashes=[])


@pytest.mark.no_postgres
def test_search_join_group_rejects_invalid_keyword_hashes() -> None:
    with pytest.raises(ValidationError, match="keyword_hashes 必须是 64 位小写 hex"):
        _payload(keywords=[], keyword_hashes=["not-a-sha256"])


@pytest.mark.no_postgres
def test_search_join_group_create_and_start_runs_precheck_and_starts(session: Session) -> None:
    task = create_and_start_search_join_group_task(session, 1, _payload(), actor="tester")

    assert task.status == "running"
    assert task.stats["started_at"]
    assert task.type_config["search_visibility_attribution"]["organic_search_join"] is True
