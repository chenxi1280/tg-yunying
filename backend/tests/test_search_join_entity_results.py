from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from app.integrations.telegram.search_join import (
    ImageVerificationDecision,
    ImageVerificationVote,
    execute_search_join_with_client,
)
from app.integrations.telegram.search_join_entity_results import (
    find_target_entity_link,
    search_result_entity_links,
)
from app.services.task_center.search_join_facts import (
    has_complete_pure_click_fact,
)


pytestmark = pytest.mark.no_postgres


@dataclass
class FakeButton:
    text: str
    data: bytes | None = None
    url: str = ""
    effect: str = ""


@dataclass
class MessageEntityTextUrl:
    url: str


@dataclass
class FakeChannel:
    id: int
    title: str
    username: str


class MessageMediaPhoto:
    photo = object()


class FakeMessage:
    def __init__(
        self,
        message_id: int,
        *,
        text: str = "",
        buttons: list[list[FakeButton]] | None = None,
        entities: list[object] | None = None,
        media: object | None = None,
    ) -> None:
        self.id = message_id
        self.raw_text = text
        self.buttons = buttons or []
        self.entities = entities or []
        self.media = media
        self.clicked: list[tuple[int, int]] = []

    async def click(self, row: int, col: int) -> None:
        self.clicked.append((row, col))


class FakeConversation:
    def __init__(self, client: "FakeClient", bot: str) -> None:
        self.client = client
        self.bot = bot

    async def __aenter__(self) -> "FakeConversation":
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def send_message(self, text: str) -> None:
        self.client.sent.append((self.bot, text))

    async def get_response(self) -> FakeMessage:
        return self.client.responses.pop(0)


class FakeClient:
    def __init__(
        self,
        responses: list[FakeMessage],
        edits: list[FakeMessage],
    ) -> None:
        self.responses = responses
        self.edits = edits
        self.sent: list[tuple[str, str]] = []
        self.history = [FakeMessage(1)]
        self.channel = FakeChannel(
            3298633687,
            "河南郑州学生会",
            "zzxshxc",
        )
        self.open_requests: list[str] = []

    def conversation(self, bot: str, timeout: int) -> FakeConversation:
        assert timeout == 60
        return FakeConversation(self, bot)

    async def get_messages(
        self,
        _bot: str,
        *,
        ids: int | None = None,
        limit: int | None = None,
    ):
        if limit is not None:
            return self.history[:limit] if not self.sent else []
        assert ids is not None
        return self.edits.pop(0)

    async def download_media(
        self,
        _message: object,
        *,
        file: type = bytes,
    ) -> bytes:
        assert file is bytes
        return b"captcha"

    async def get_entity(self, target: str) -> FakeChannel:
        assert target == "https://t.me/zzxshxc"
        return self.channel

    async def __call__(self, request):
        assert request.__class__.__name__ == "GetFullChannelRequest"
        self.open_requests.append(request.__class__.__name__)
        return SimpleNamespace(chats=[self.channel])


def _profile() -> dict:
    return {
        "page_fingerprints": [
            {
                "page_phase": "verification_page",
                "text_enums": ["human_verification"],
            },
            {
                "page_phase": "hot_list_page",
                "text_enums": ["hot_list"],
            },
            {
                "page_phase": "search_category_page",
                "button_text_enums_any": [
                    "jisou_group_category",
                    "jisou_channel_category",
                ],
                "selector_rules": [{
                    "row": 0,
                    "col": 0,
                    "button_type": "callback_data",
                    "effect": "unknown",
                    "normalized_text": "jisou_group_category",
                }],
            },
            {
                "page_phase": "group_result_page",
                "button_effects_any": [
                    "navigate_only",
                    "target_open_only",
                ],
                "membership_side_effects_allowed": ["none"],
            },
        ]
    }


def _payload() -> dict:
    return {
        "bot_username": "jisou",
        "keyword_hash": "a" * 64,
        "target_username": "zzxshxc",
        "target_title": "河南郑州学生会",
        "search_execution_mode": "click_only",
        "safe_navigation": {
            "pre_join_decoy_click_max": 0,
            "post_join_safe_navigation_max": 0,
            "total_max": 0,
        },
        "approved_protocol_profile": _profile(),
    }


def _hot_list(message_id: int) -> FakeMessage:
    return FakeMessage(
        message_id,
        text="热搜排行榜 郑州",
        buttons=[
            [FakeButton("👥", data=b"group")],
            [FakeButton("📢", data=b"channel")],
        ],
        entities=[
            MessageEntityTextUrl("https://t.me/not_target"),
        ],
    )


def _group_page(
    message_id: int,
    usernames: list[str],
    *,
    with_next: bool,
) -> FakeMessage:
    buttons = [[FakeButton("🔄", data=b"refresh")]]
    if with_next:
        buttons.append([
            FakeButton("➡️", data=b"next", effect="navigate_only")
        ])
    return FakeMessage(
        message_id,
        text="\n".join(f"👥{item}" for item in usernames),
        buttons=buttons,
        entities=[
            MessageEntityTextUrl(f"https://t.me/{item}")
            for item in usernames
        ],
    )


def _entity_click_fact(result: dict) -> dict:
    return {
        **result,
        "target_click_observed_at": "2026-07-30T16:00:00+00:00",
    }


def test_parser_uses_exact_text_url_username() -> None:
    message = FakeMessage(
        10,
        entities=[
            MessageEntityTextUrl("https://example.com/zzxshxc"),
            MessageEntityTextUrl(
                "https://t.me/jisou1Bot?start=tracking"
            ),
            MessageEntityTextUrl("https://t.me/zzxshxc"),
        ],
    )

    links = search_result_entity_links(message)
    target = find_target_entity_link(links, "@zzxshxc")

    assert [link.username for link in links] == ["zzxshxc"]
    assert target is not None
    assert target.position == 1


def test_full_flow_selects_group_paginates_and_opens_target() -> None:
    hot_list = _hot_list(100)
    first_group_page = _group_page(
        100,
        ["other_group"],
        with_next=True,
    )
    target_page = _group_page(
        100,
        ["other_again", "zzxshxc"],
        with_next=False,
    )
    client = FakeClient(
        [hot_list],
        [first_group_page, target_page],
    )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(),
            keyword_text="郑州",
        )
    )

    assert result["success"] is True
    assert client.sent == [("jisou", "郑州")]
    assert hot_list.clicked == [(0, 0)]
    assert first_group_page.clicked == [(1, 0)]
    assert client.open_requests == ["GetFullChannelRequest"]
    assert result["target_position"] == 2
    assert result["target_entity_id"] == "3298633687"
    assert result["target_entity_username"] == "zzxshxc"
    assert result["target_open_rpc"] == (
        "channels.GetFullChannelRequest"
    )
    assert result["membership_mutating_rpc_invoked"] is False
    assert result["jisou_post_verification_keyword_replayed"] is False
    assert has_complete_pure_click_fact(
        _entity_click_fact(result)
    )


def test_verification_success_selects_group_without_keyword_replay() -> None:
    answers = ["0", "1", "2", "3", "4", "5", "6", "7"]
    verification = FakeMessage(
        100,
        text="人机验证 请选择计算结果",
        buttons=[[
            FakeButton(answer, data=answer.encode())
            for answer in answers
        ]],
        media=MessageMediaPhoto(),
    )
    hot_list = _hot_list(101)
    target_page = _group_page(
        101,
        ["zzxshxc"],
        with_next=False,
    )
    client = FakeClient(
        [verification],
        [hot_list, target_page],
    )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(),
            keyword_text="郑州",
            image_verification_solver=lambda _request: ImageVerificationDecision(
                "0",
                0.95,
                (
                    ImageVerificationVote(
                        "model", "accepted", "0", 0.95, True
                    ),
                    ImageVerificationVote(
                        "tesseract", "accepted", "0", 0.80, True
                    ),
                    ImageVerificationVote(
                        "rapidocr", "unsafe", "", 0.0, False
                    ),
                ),
            ),
        )
    )

    assert result["success"] is True
    assert verification.clicked == [(0, 0)]
    assert hot_list.clicked == [(0, 0)]
    assert client.sent == [("jisou", "郑州")]
    assert result["jisou_post_verification_keyword_replayed"] is False


def test_entity_fact_requires_remote_open_rpc() -> None:
    result = {
        "target_click_observed": True,
        "membership_side_effect": "none",
        "membership_mutating_rpc_invoked": False,
        "target_username": "zzxshxc",
        "bot_username": "jisou",
        "keyword_hash": "a" * 64,
        "target_message_id": "100",
        "target_position": 1,
        "target_button_type": "message_entity_text_url",
        "target_button_effect": "target_open_only",
        "target_button_fingerprint": "fingerprint",
        "target_click_observed_at": "2026-07-30T16:00:00+00:00",
        "target_entity_url_hash": "url-hash",
        "target_entity_id": "3298633687",
        "target_entity_username": "zzxshxc",
        "target_entity_title_hash": "title-hash",
        "target_open_rpc": "",
    }

    assert has_complete_pure_click_fact(result) is False
