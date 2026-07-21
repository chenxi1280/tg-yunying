from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.integrations.telegram.search_join import execute_search_join_with_client


@dataclass
class FakeButton:
    text: str
    url: str = ""
    data: bytes | None = None
    effect: str = ""


class FakeMessage:
    def __init__(
        self,
        message_id: int,
        buttons: list[list[FakeButton]],
        click_results: dict[tuple[int, int], object] | None = None,
        raw_text: str = "",
    ) -> None:
        self.id = message_id
        self.buttons = buttons
        self.clicked: list[tuple[int, int]] = []
        self.click_results = click_results or {}
        self.raw_text = raw_text

    async def click(self, row: int, col: int):
        self.clicked.append((row, col))
        return self.click_results.get((row, col))


class FakeConversation:
    def __init__(self, client: "FakeSearchJoinClient", bot: str) -> None:
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


class FakeSearchJoinClient:
    def __init__(
        self,
        responses: list[FakeMessage],
        join_error: Exception | None = None,
        edits: list[FakeMessage] | None = None,
    ) -> None:
        self.responses = responses
        self.join_error = join_error
        self.edits = edits or []
        self.updated_message_ids: list[int] = []
        self.sent: list[tuple[str, str]] = []
        self.joined: list[str] = []
        self.imported_invites: list[str] = []
        self.read_targets: list[str] = []

    def conversation(self, bot: str, timeout: int):
        assert timeout == 60
        return FakeConversation(self, bot)

    async def get_messages(self, _bot: str, ids: int) -> FakeMessage:
        self.updated_message_ids.append(ids)
        if self.edits:
            return self.edits.pop(0)
        return self.responses.pop(0)

    async def get_entity(self, target: str):
        return target

    async def mark_read(self, target: str) -> None:
        self.read_targets.append(target)

    async def __call__(self, request):
        name = request.__class__.__name__
        if name == "JoinChannelRequest":
            if self.join_error:
                raise self.join_error
            self.joined.append(str(request.channel))
        if name == "ImportChatInviteRequest":
            self.imported_invites.append(str(request.hash))
        return None


@dataclass
class FakeCallbackAnswer:
    url: str


class FakeAlreadyParticipantError(Exception):
    def __str__(self) -> str:
        return "The authenticated user is already a participant of the channel"


def _payload(**overrides) -> dict:
    payload = {
        "bot_username": "searchbot",
        "keyword_hash": "a" * 64,
        "target_username": "target_group",
        "target_group_id": 17,
        "safe_navigation": {"pre_join_decoy_click_max": 1, "post_join_safe_navigation_max": 0, "total_max": 1},
        "post_join_policy": "stay_joined",
    }
    payload.update(overrides)
    return payload


@pytest.mark.no_postgres
def test_execute_search_join_sends_keyword_clicks_safe_navigation_and_joins_target() -> None:
    safe = FakeButton("看看介绍", data=b"safe", effect="navigate_only")
    target = FakeButton("目标群", url="https://t.me/target_group")
    message = FakeMessage(101, [[safe], [target]])
    client = FakeSearchJoinClient([FakeMessage(100, []), message])

    result = asyncio.run(execute_search_join_with_client(client, _payload(), keyword_text="上海 留学"))

    assert result["success"] is True
    assert client.sent == [("searchbot", "/start"), ("searchbot", "上海 留学")]
    assert message.clicked == [(0, 0), (1, 0)]
    assert client.joined == ["target_group"]
    assert client.read_targets == ["target_group"]
    assert result["join_status"] == "membership_observed"
    assert result["pre_join_decoy_clicks"][0]["joined"] is False
    assert "上海 留学" not in str(result)


@pytest.mark.no_postgres
def test_execute_search_join_selects_jisou_group_category_before_pagination() -> None:
    category_page = FakeMessage(
        101,
        [[FakeButton("👥", data=b"group-category")], [FakeButton("📢", data=b"channel-category")]],
    )
    first_group_page = FakeMessage(
        102,
        [[FakeButton("其他群一", url="https://t.me/other_1")], [FakeButton("下一页", data=b"next-1", effect="navigate_only")]],
    )
    second_group_page = FakeMessage(
        103,
        [[FakeButton("其他群二", url="https://t.me/other_2")], [FakeButton("下一页", data=b"next-2", effect="navigate_only")]],
    )
    third_group_page = FakeMessage(
        104,
        [[FakeButton("其他群三", url="https://t.me/other_3")], [FakeButton("下一页", data=b"next-3", effect="navigate_only")]],
    )
    target_page = FakeMessage(105, [[FakeButton("目标群", url="https://t.me/target_group")]])
    client = FakeSearchJoinClient(
        [FakeMessage(100, []), category_page, first_group_page, second_group_page, third_group_page, target_page]
    )

    result = asyncio.run(execute_search_join_with_client(client, _payload(bot_username="jisou"), keyword_text="郑州"))

    assert result["success"] is True
    assert category_page.clicked == [(0, 0)]
    assert first_group_page.clicked == [(1, 0)]
    assert second_group_page.clicked == [(1, 0)]
    assert third_group_page.clicked == [(1, 0)]
    assert target_page.clicked == [(0, 0)]
    assert result["page"] == 4
    assert result["searched_pages"] == 4


@pytest.mark.no_postgres
def test_execute_search_join_reads_jisou_callback_edit_instead_of_unrelated_new_message() -> None:
    category_page = FakeMessage(101, [[FakeButton("👥", data=b"group-category")]])
    filtered_results_page = FakeMessage(102, [[FakeButton("目标群", url="https://t.me/target_group")]])
    unrelated_message = FakeMessage(103, [[FakeButton("其他群", url="https://t.me/other_group")]])
    client = FakeSearchJoinClient(
        [FakeMessage(100, []), category_page, unrelated_message],
        edits=[filtered_results_page],
    )

    result = asyncio.run(execute_search_join_with_client(client, _payload(bot_username="jisou"), keyword_text="郑州"))

    assert result["success"] is True
    assert client.updated_message_ids == [101]
    assert client.responses == [unrelated_message]


@pytest.mark.no_postgres
def test_execute_search_join_rejects_unfiltered_jisou_results_when_group_selector_is_missing() -> None:
    result_page = FakeMessage(101, [[FakeButton("目标群", url="https://t.me/target_group")]])
    client = FakeSearchJoinClient([FakeMessage(100, []), result_page])

    result = asyncio.run(execute_search_join_with_client(client, _payload(bot_username="jisou"), keyword_text="郑州"))

    assert result["success"] is False
    assert result["error_code"] == "jisou_group_selector_missing"
    assert result_page.clicked == []
    assert client.joined == []


@pytest.mark.no_postgres
def test_execute_search_join_never_uses_exact_target_as_pre_join_decoy() -> None:
    target = FakeButton("目标群", url="https://t.me/target_group", effect="navigate_only")
    message = FakeMessage(101, [[target]])
    client = FakeSearchJoinClient([FakeMessage(100, []), message])

    result = asyncio.run(execute_search_join_with_client(client, _payload(), keyword_text="上海 留学"))

    assert result["success"] is True
    assert message.clicked == [(0, 0)]
    assert result["pre_join_decoy_clicks"] == []


@pytest.mark.no_postgres
def test_execute_search_join_rejects_peer_only_target() -> None:
    target = FakeButton("郑州平价资源（交流群）", data=b"target")
    message = FakeMessage(101, [[target]], click_results={(0, 0): FakeCallbackAnswer("https://t.me/xiaozisk")})
    client = FakeSearchJoinClient([FakeMessage(100, []), message])

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(target_username="", target_peer_id="-1002188784621"),
            keyword_text="郑州平价资源",
        )
    )

    assert result["error_code"] == "target_identity_missing"
    assert message.clicked == []
    assert client.joined == []


@pytest.mark.no_postgres
def test_execute_search_join_never_treats_peer_only_target_as_decoy() -> None:
    target = FakeButton("目标详情", url="https://t.me/xiaozisk", effect="navigate_only")
    message = FakeMessage(101, [[target]])
    client = FakeSearchJoinClient([FakeMessage(100, []), message])

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(target_username="", target_peer_id="-1002188784621"),
            keyword_text="郑州平价资源",
        )
    )

    assert result["error_code"] == "target_identity_missing"
    assert message.clicked == []


@pytest.mark.no_postgres
def test_execute_search_join_joins_known_target_when_message_text_matches() -> None:
    result_page = FakeMessage(
        101,
        [[FakeButton("👥", data=b"group-category")], [FakeButton("下一页", data=b"next", effect="navigate_only")]],
        raw_text="👥郑州平价资源（交流群） @xiaozisk 46k\n📢其他频道 2k",
    )
    client = FakeSearchJoinClient([FakeMessage(100, []), result_page])

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(target_username="xiaozisk", target_title="郑州平价资源（交流群）", target_peer_id="-1002188784621"),
            keyword_text="郑州",
        )
    )

    assert result["success"] is True
    assert client.joined == ["xiaozisk"]
    assert result["target_match_source"] == "message_text"
    assert result["target_line"] == "👥郑州平价资源（交流群） @xiaozisk 46k"


@pytest.mark.no_postgres
def test_execute_search_join_counts_target_click_success_when_account_already_joined() -> None:
    target = FakeButton("目标群", url="https://t.me/target_group")
    message = FakeMessage(101, [[target]])
    client = FakeSearchJoinClient([FakeMessage(100, []), message], join_error=FakeAlreadyParticipantError())

    result = asyncio.run(execute_search_join_with_client(client, _payload(), keyword_text="上海 留学"))

    assert result["success"] is True
    assert message.clicked == [(0, 0)]
    assert client.read_targets == ["target_group"]
    assert result["join_status"] == "membership_observed"


@pytest.mark.no_postgres
def test_execute_search_join_reports_bot_human_verification() -> None:
    captcha_page = FakeMessage(101, [[FakeButton("42", data=b"answer")]], raw_text="您必须完成人机验证才能继续使用\n请选择计算结果")
    client = FakeSearchJoinClient([FakeMessage(100, []), captcha_page])

    result = asyncio.run(execute_search_join_with_client(client, _payload(), keyword_text="郑州"))

    assert result["success"] is False
    assert result["error_code"] == "bot_human_verification_required"
    assert captcha_page.clicked == []


@pytest.mark.no_postgres
def test_execute_search_join_reports_target_not_in_results_without_joining() -> None:
    message = FakeMessage(101, [[FakeButton("其他群", url="https://t.me/other_group")]])
    client = FakeSearchJoinClient([FakeMessage(100, []), message])

    result = asyncio.run(execute_search_join_with_client(client, _payload(), keyword_text="上海 留学"))

    assert result["success"] is False
    assert result["error_code"] == "target_not_in_results"
    assert result["searched_pages"] == 1
    assert result["last_result_page"] == 1
    assert result["search_end_reason"] == "no_next_page"
    assert "pages_exhausted" not in result
    assert client.joined == []


@pytest.mark.no_postgres
def test_execute_search_join_does_not_bind_external_url_without_username() -> None:
    message = FakeMessage(101, [[FakeButton("目标外链", url="https://example.com/target_group")]])
    client = FakeSearchJoinClient([FakeMessage(100, []), message])

    result = asyncio.run(execute_search_join_with_client(client, _payload(), keyword_text="上海 留学"))

    assert result["success"] is False
    assert result["error_code"] == "target_not_in_results"
    assert client.joined == []


@pytest.mark.no_postgres
def test_execute_search_join_does_not_click_unbound_callback_target() -> None:
    target = FakeButton("目标群", data=b"target-callback")
    message = FakeMessage(101, [[target]], click_results={(0, 0): FakeCallbackAnswer("https://t.me/target_group")})
    client = FakeSearchJoinClient([FakeMessage(100, []), message])

    result = asyncio.run(
        execute_search_join_with_client(client, _payload(), keyword_text="上海 留学")
    )

    assert result["error_code"] == "target_not_in_results"
    assert message.clicked == []
    assert client.joined == []


@pytest.mark.no_postgres
def test_execute_search_join_does_not_import_unbound_private_invite() -> None:
    target = FakeButton("目标群", url="https://t.me/+inviteHash")
    message = FakeMessage(101, [[target]])
    client = FakeSearchJoinClient([FakeMessage(100, []), message])

    result = asyncio.run(
        execute_search_join_with_client(client, _payload(), keyword_text="上海 留学")
    )

    assert result["error_code"] == "target_not_in_results"
    assert client.imported_invites == []


@pytest.mark.no_postgres
def test_execute_search_join_navigates_pages_until_target_found() -> None:
    next_button = FakeButton("下一页 »", data=b"next", effect="navigate_only")
    first_page = FakeMessage(101, [[FakeButton("其他群", url="https://t.me/other_group")], [next_button]])
    second_page = FakeMessage(102, [[FakeButton("目标群", url="https://t.me/target_group")]])
    client = FakeSearchJoinClient([FakeMessage(100, []), first_page, second_page])

    result = asyncio.run(execute_search_join_with_client(client, _payload(), keyword_text="上海 留学"))

    assert result["success"] is True
    assert first_page.clicked == [(1, 0)]
    assert second_page.clicked == [(0, 0)]
    assert result["target_position"] == 1
    assert result["page"] == 2


@pytest.mark.no_postgres
def test_execute_search_join_searches_beyond_legacy_page_70_until_target_found() -> None:
    pages = [
        FakeMessage(100 + page_no, [[FakeButton("其他群", url=f"https://t.me/other_{page_no}")], [FakeButton("下一页 »", data=b"next", effect="navigate_only")]])
        for page_no in range(1, 71)
    ]
    target_page = FakeMessage(171, [[FakeButton("目标群", url="https://t.me/target_group")]])
    client = FakeSearchJoinClient([FakeMessage(100, []), *pages, target_page])

    result = asyncio.run(execute_search_join_with_client(client, _payload(), keyword_text="上海 留学"))

    assert result["success"] is True
    assert result["page"] == 71
    assert result["searched_pages"] == 71
    assert all(page.clicked == [(1, 0)] for page in pages)
    assert client.joined == ["target_group"]


@pytest.mark.no_postgres
def test_execute_search_join_reports_actual_last_page_without_legacy_page_cap() -> None:
    first_page = FakeMessage(101, [[FakeButton("其他群一", url="https://t.me/other_1")], [FakeButton("下一页 »", data=b"next", effect="navigate_only")]])
    last_page = FakeMessage(102, [[FakeButton("其他群二", url="https://t.me/other_2")]])
    pages = [first_page, last_page]
    client = FakeSearchJoinClient([FakeMessage(100, []), *pages])

    result = asyncio.run(execute_search_join_with_client(client, _payload(max_pages=70), keyword_text="上海 留学"))

    assert result["success"] is False
    assert result["error_code"] == "target_not_in_results"
    assert result["page"] == 2
    assert result["searched_pages"] == 2
    assert result["last_result_page"] == 2
    assert result["search_end_reason"] == "no_next_page"
    assert "pages_exhausted" not in result
    assert result["pre_join_decoy_clicks"] == []
    assert client.joined == []


@pytest.mark.no_postgres
def test_execute_search_join_records_sanitized_jisou_page_structure_when_no_next_page_exists() -> None:
    category_page = FakeMessage(
        101,
        [[FakeButton("👥", data=b"group-category")], [FakeButton("📢", data=b"channel-category")]],
    )
    result_page = FakeMessage(
        102,
        [[FakeButton("其他群", url="https://t.me/other_group")], [FakeButton("▶", data=b"next-page")]],
    )
    client = FakeSearchJoinClient([FakeMessage(100, []), category_page, result_page])

    result = asyncio.run(execute_search_join_with_client(client, _payload(bot_username="jisou"), keyword_text="郑州"))

    assert result["error_code"] == "target_not_in_results"
    assert result["search_protocol_trace"] == {
        "jisou_group_selector": {"position": 1, "text": "👥"},
        "selector_page": {
            "button_count": 2,
            "button_layout": [
                {
                    "row": 0,
                    "col": 0,
                    "button_type": "callback_data",
                    "effect": "unknown",
                    "text_length": 1,
                    "contains_page_marker": False,
                    "navigation_symbols": [],
                },
                {
                    "row": 1,
                    "col": 0,
                    "button_type": "callback_data",
                    "effect": "unknown",
                    "text_length": 1,
                    "contains_page_marker": False,
                    "navigation_symbols": [],
                },
            ],
        },
        "result_page": {
            "button_count": 2,
            "button_layout": [
                {
                    "row": 0,
                    "col": 0,
                    "button_type": "telegram_url",
                    "effect": "join_candidate",
                    "text_length": 3,
                    "contains_page_marker": False,
                    "navigation_symbols": [],
                },
                {
                    "row": 1,
                    "col": 0,
                    "button_type": "callback_data",
                    "effect": "unknown",
                    "text_length": 1,
                    "contains_page_marker": False,
                    "navigation_symbols": ["right_triangle"],
                },
            ],
        },
    }
