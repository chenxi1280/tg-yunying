from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.integrations.telegram.gateway import TelethonTelegramGateway
from app.models import BotProtocolSample, Tenant
from app.services.task_center.search_rank_deboost import (
    require_rank_observation_gateway,
    validate_rank_deboost_protocol_samples,
)


pytestmark = pytest.mark.no_postgres


def _make_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    session.commit()
    return session


def _add_sample(
    session: Session,
    *,
    sample_type: str,
    bot_username: str = "jisou",
    sample_purpose: str = "rank_deboost",
    is_active: bool = True,
    structure_json: dict | None = None,
) -> BotProtocolSample:
    sample = BotProtocolSample(
        tenant_id=1,
        bot_username=bot_username,
        sample_type=sample_type,
        sample_purpose=sample_purpose,
        is_active=is_active,
        structure_json=structure_json or {},
    )
    session.add(sample)
    return sample


def _seed_sufficient(session: Session) -> None:
    """插入达阈值的全量样本：start_response×2、search_results×5、pagination_response×3、
    button_structure 含 3 种 button_effect、exit_ip_observation×3。"""
    for _ in range(2):
        _add_sample(session, sample_type="start_response")
    for _ in range(5):
        _add_sample(session, sample_type="search_results")
    for _ in range(3):
        _add_sample(session, sample_type="pagination_response")
    for effect in ("navigate_only", "join_candidate", "external_http_url"):
        _add_sample(
            session,
            sample_type="button_structure",
            structure_json={"button_effect": effect},
        )
    for _ in range(3):
        _add_sample(session, sample_type="exit_ip_observation")


def test_validate_passes_when_all_samples_sufficient() -> None:
    """所有样本达阈值，不 raise。"""
    session = _make_session()
    _seed_sufficient(session)
    session.commit()

    validate_rank_deboost_protocol_samples(session, 1, "jisou")


def test_validate_fails_when_start_response_insufficient() -> None:
    """start_response < 2，raise ValueError 含 'start_response'。"""
    session = _make_session()
    _seed_sufficient(session)
    session.query(BotProtocolSample).filter(
        BotProtocolSample.sample_type == "start_response"
    ).delete()
    _add_sample(session, sample_type="start_response")
    session.commit()

    with pytest.raises(ValueError, match="start_response"):
        validate_rank_deboost_protocol_samples(session, 1, "jisou")


def test_validate_fails_when_search_results_insufficient() -> None:
    """search_results < 5，raise ValueError 含 'search_results'。"""
    session = _make_session()
    _seed_sufficient(session)
    session.query(BotProtocolSample).filter(
        BotProtocolSample.sample_type == "search_results"
    ).delete()
    for _ in range(4):
        _add_sample(session, sample_type="search_results")
    session.commit()

    with pytest.raises(ValueError, match="search_results"):
        validate_rank_deboost_protocol_samples(session, 1, "jisou")


def test_validate_fails_when_pagination_insufficient() -> None:
    """pagination_response < 3，raise ValueError 含 'pagination_response'。"""
    session = _make_session()
    _seed_sufficient(session)
    session.query(BotProtocolSample).filter(
        BotProtocolSample.sample_type == "pagination_response"
    ).delete()
    for _ in range(2):
        _add_sample(session, sample_type="pagination_response")
    session.commit()

    with pytest.raises(ValueError, match="pagination_response"):
        validate_rank_deboost_protocol_samples(session, 1, "jisou")


def test_validate_fails_when_button_structure_effect_types_insufficient() -> None:
    """button_structure 样本中 button_effect 去重后 < 3 种，raise ValueError。"""
    session = _make_session()
    _seed_sufficient(session)
    session.query(BotProtocolSample).filter(
        BotProtocolSample.sample_type == "button_structure"
    ).delete()
    for effect in ("navigate_only", "join_candidate"):
        _add_sample(
            session,
            sample_type="button_structure",
            structure_json={"button_effect": effect},
        )
    session.commit()

    with pytest.raises(ValueError, match="button_structure"):
        validate_rank_deboost_protocol_samples(session, 1, "jisou")


def test_validate_fails_when_exit_ip_observation_insufficient() -> None:
    """exit_ip_observation < 3，raise ValueError 含 'exit_ip_observation'。"""
    session = _make_session()
    _seed_sufficient(session)
    session.query(BotProtocolSample).filter(
        BotProtocolSample.sample_type == "exit_ip_observation"
    ).delete()
    for _ in range(2):
        _add_sample(session, sample_type="exit_ip_observation")
    session.commit()

    with pytest.raises(ValueError, match="exit_ip_observation"):
        validate_rank_deboost_protocol_samples(session, 1, "jisou")


def test_validate_ignores_inactive_samples() -> None:
    """is_active=false 的样本不计入。"""
    session = _make_session()
    _seed_sufficient(session)
    session.query(BotProtocolSample).filter(
        BotProtocolSample.sample_type == "start_response"
    ).update({BotProtocolSample.is_active: False})
    _add_sample(session, sample_type="start_response", is_active=True)
    session.commit()

    with pytest.raises(ValueError, match="start_response"):
        validate_rank_deboost_protocol_samples(session, 1, "jisou")


def test_validate_ignores_search_join_purpose_samples() -> None:
    """sample_purpose='search_join' 的样本不计入。"""
    session = _make_session()
    _seed_sufficient(session)
    session.query(BotProtocolSample).filter(
        BotProtocolSample.sample_type == "start_response"
    ).update({BotProtocolSample.sample_purpose: "search_join"})
    _add_sample(session, sample_type="start_response")
    session.commit()

    with pytest.raises(ValueError, match="start_response"):
        validate_rank_deboost_protocol_samples(session, 1, "jisou")


def test_validate_ignores_non_jisou_bots() -> None:
    """bot_username != 'jisou' 的样本不计入。"""
    session = _make_session()
    _seed_sufficient(session)
    session.query(BotProtocolSample).filter(
        BotProtocolSample.sample_type == "start_response"
    ).update({BotProtocolSample.bot_username: "other_bot"})
    _add_sample(session, sample_type="start_response", bot_username="jisou")
    session.commit()

    with pytest.raises(ValueError, match="start_response"):
        validate_rank_deboost_protocol_samples(session, 1, "jisou")


@dataclass
class FakeButton:
    text: str
    effect: str
    url: str = ""


class FakeMessage:
    def __init__(self, text: str, rows: list[list[FakeButton]]) -> None:
        self.message = text
        self.buttons = rows
        self.clicks: list[tuple[int, int]] = []


class FakeConversation:
    def __init__(self, pages: list[FakeMessage]) -> None:
        self.pages = pages
        self.index = 0

    async def __aenter__(self) -> "FakeConversation":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def send_message(self, _text: str) -> None:
        return None

    async def get_response(self) -> FakeMessage:
        item = self.pages[self.index]
        self.index += 1
        return item


class FakeClient:
    def __init__(self, pages: list[FakeMessage]) -> None:
        self.pages = pages

    def conversation(self, _bot: str, timeout: int = 60) -> FakeConversation:
        return FakeConversation(self.pages)


def _payload() -> dict:
    return {
        "bot_username": "jisou",
        "target_group_ids": [1001],
        "exempt_group_username": "exempt_group",
        "runtime_environment": {"runtime_proxy_id": "31", "observed_exit_ip": "8.8.8.8"},
    }


def test_rank_observation_gateway_accepts_production_class_without_monkeypatch() -> None:
    require_rank_observation_gateway(TelethonTelegramGateway(Settings(telethon_operation_timeout_seconds=1)))


def test_rank_deboost_candidate_search_parses_positions_without_clicking() -> None:
    from app.integrations.telegram.search_rank_deboost import search_rank_deboost_candidates_with_client

    page = FakeMessage("1. @competitor_a\n2. @my_target\n3. @competitor_b", [])
    client = FakeClient([FakeMessage("start", []), page])

    result = asyncio.run(search_rank_deboost_candidates_with_client(client, _payload(), keyword_text="keyword"))

    assert result["success"] is True
    assert [item["username"] for item in result["search_results"]] == ["competitor_a", "my_target", "competitor_b"]
    assert page.clicks == []
