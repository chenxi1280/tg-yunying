from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import dataclass
from time import monotonic

import pytest

from app.integrations.telegram import search_join as search_join_module
from app.integrations.telegram.search_join import (
    ImageVerificationConsensusUnavailableError,
    ImageVerificationDecision,
    ImageVerificationNoSafeAnswerError,
    ImageVerificationProviderUnavailableError,
    ImageVerificationRuntimeContractError,
    ImageVerificationVote,
    ensure_search_join_membership_with_client,
    execute_search_join_with_client,
    probe_search_join_membership_with_client,
)


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
        media: object | None = None,
    ) -> None:
        self.id = message_id
        self.buttons = buttons
        self.clicked: list[tuple[int, int]] = []
        self.click_results = click_results or {}
        self.raw_text = raw_text
        self.media = media

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
        membership_probe_error: Exception | None = None,
        edits: list[FakeMessage] | None = None,
        latest_messages: list[FakeMessage] | None = None,
        history_messages: list[FakeMessage] | None = None,
        download_media_result: bytes = b"\x89PNG fake image",
    ) -> None:
        self.responses = responses
        self.join_error = join_error
        self.membership_probe_error = membership_probe_error
        self.edits = edits or []
        self.latest_messages = latest_messages or []
        self.history_messages = history_messages or []
        self.updated_message_ids: list[int] = []
        self.sent: list[tuple[str, str]] = []
        self.joined: list[str] = []
        self.imported_invites: list[str] = []
        self.read_targets: list[str] = []
        self.download_media_result = download_media_result
        self.download_media_calls = 0

    async def download_media(self, _message: object, *, file: type = bytes) -> bytes:
        self.download_media_calls += 1
        return self.download_media_result

    def conversation(self, bot: str, timeout: int):
        assert timeout == 60
        return FakeConversation(self, bot)

    async def get_messages(
        self,
        _bot: str,
        ids: int | None = None,
        limit: int | None = None,
    ) -> FakeMessage | list[FakeMessage] | None:
        if limit is not None:
            if not self.sent:
                return self.history_messages[:limit]
            return self.latest_messages[:limit]
        assert ids is not None
        self.updated_message_ids.append(ids)
        if self.edits:
            return self.edits.pop(0)
        return self.responses.pop(0) if self.responses else None

    async def get_entity(self, target: str):
        return target

    async def get_me(self, input_peer: bool = False):
        assert input_peer is True
        return "me"

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
        if name == "GetParticipantRequest" and self.membership_probe_error:
            raise self.membership_probe_error
        return None


@dataclass
class FakeCallbackAnswer:
    url: str


class FakeAlreadyParticipantError(Exception):
    def __str__(self) -> str:
        return "The authenticated user is already a participant of the channel"


class FakeJoinRequestPendingError(Exception):
    def __str__(self) -> str:
        return "You have successfully requested to join this chat or channel (caused by JoinChannelRequest)"


class FakeNotParticipantError(Exception):
    def __str__(self) -> str:
        return "User is not a participant of the channel"


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
    if str(payload.get("bot_username") or "").lstrip("@").lower() == "jisou" and "approved_protocol_profile" not in overrides:
        payload["approved_protocol_profile"] = _jisou_protocol_profile()
    return payload


def _jisou_protocol_profile() -> dict:
    return {
        "page_fingerprints": [
            {"page_phase": "verification_page", "text_enums": ["human_verification"]},
            {"page_phase": "hot_list_page", "text_enums": ["hot_list"]},
            {
                "page_phase": "search_category_page",
                "button_text_enums_any": ["jisou_group_category", "jisou_channel_category"],
                "selector_rules": [
                    {
                        "row": 0,
                        "col": 0,
                        "button_type": "callback_data",
                        "effect": "unknown",
                        "normalized_text": "jisou_group_category",
                    }
                ],
            },
            {"page_phase": "group_result_page", "button_effects_any": ["join_candidate", "navigate_only"]},
        ]
    }


def _pure_click_protocol_profile() -> dict:
    profile = _jisou_protocol_profile()
    result_page = profile["page_fingerprints"][-1]
    result_page["button_effects_any"] = [
        "navigate_only",
        "target_open_only",
    ]
    result_page["membership_side_effects_allowed"] = ["none"]
    return profile


@pytest.mark.no_postgres
def test_execute_search_join_sends_keyword_clicks_safe_navigation_and_marks_target_found() -> None:
    safe = FakeButton("看看介绍", data=b"safe", effect="navigate_only")
    target = FakeButton("目标群", url="https://t.me/target_group")
    message = FakeMessage(101, [[safe], [target]])
    client = FakeSearchJoinClient([FakeMessage(100, []), message])

    result = asyncio.run(execute_search_join_with_client(client, _payload(), keyword_text="上海 留学"))

    assert result["success"] is True
    assert client.sent == [("searchbot", "/start"), ("searchbot", "上海 留学")]
    assert message.clicked == [(0, 0), (1, 0)]
    assert client.joined == []
    assert client.read_targets == []
    assert result["join_status"] == "target_found"
    assert result["pre_join_decoy_clicks"][0]["joined"] is False
    assert "上海 留学" not in str(result)


@pytest.mark.no_postgres
def test_execute_search_join_rejects_jisou_without_approved_profile() -> None:
    category_page = FakeMessage(101, [[FakeButton("👥", data=b"group-category")]])
    client = FakeSearchJoinClient([FakeMessage(100, []), category_page])

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou", approved_protocol_profile={}),
            keyword_text="郑州",
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "jisou_session_state_deviated"
    assert result["jisou_page_phase"] == "unknown_page"
    assert category_page.clicked == []


@pytest.mark.no_postgres
def test_execute_search_join_marks_buttons_matched_to_the_approved_profile() -> None:
    category_page = FakeMessage(101, [[FakeButton("👥", data=b"group-category")]])
    result_page = FakeMessage(102, [[FakeButton("目标群", url="https://t.me/target_group")]])
    client = FakeSearchJoinClient([FakeMessage(100, []), category_page, result_page])

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou", approved_protocol_profile=_jisou_protocol_profile()),
            keyword_text="郑州",
        )
    )

    assert result["success"] is True
    assert result["search_protocol_trace"]["selector_page"]["button_layout"][0]["approved_sample_match"] is True
    assert result["search_protocol_trace"]["result_page"]["button_layout"][0]["approved_sample_match"] is True


@pytest.mark.no_postgres
def test_pure_search_click_records_complete_fact_without_joining() -> None:
    category_page = FakeMessage(
        101,
        [[FakeButton("👥", data=b"group-category")]],
    )
    target_page = FakeMessage(
        102,
        [[FakeButton(
            "目标群",
            url="https://t.me/target_group",
        )]],
    )
    client = FakeSearchJoinClient([
        FakeMessage(100, []),
        category_page,
        target_page,
    ])
    payload = _payload(
        bot_username="jisou",
        search_execution_mode="click_only",
        approved_protocol_profile=_pure_click_protocol_profile(),
    )

    result = asyncio.run(
        execute_search_join_with_client(client, payload, keyword_text="郑州")
    )

    assert result["success"] is True
    assert result["target_click_observed"] is True
    assert result["target_button_effect"] == "target_open_only"
    assert result["membership_side_effect"] == "none"
    assert result["membership_mutating_rpc_invoked"] is False
    assert client.joined == []


@pytest.mark.no_postgres
def test_search_join_membership_applies_after_target_found() -> None:
    client = FakeSearchJoinClient([])

    result = asyncio.run(ensure_search_join_membership_with_client(client, _payload()))

    assert result["success"] is True
    assert result["join_status"] == "membership_observed"
    assert client.joined == ["target_group"]


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
def test_execute_search_join_follows_jisou_right_arrow_until_target_is_found_on_page_four() -> None:
    category_page = FakeMessage(101, [[FakeButton("👥", data=b"group-category")]])
    first_page = FakeMessage(102, [[FakeButton("其他群一", url="https://t.me/other_1")], [FakeButton("➡️", data=b"next-1")]])
    second_page = FakeMessage(103, [[FakeButton("其他群二", url="https://t.me/other_2")], [FakeButton("➡️", data=b"next-2")]])
    third_page = FakeMessage(104, [[FakeButton("其他群三", url="https://t.me/other_3")], [FakeButton("➡️", data=b"next-3")]])
    target_page = FakeMessage(105, [[FakeButton("目标群", url="https://t.me/target_group")]])
    client = FakeSearchJoinClient([FakeMessage(100, []), category_page, first_page, second_page, third_page, target_page])

    result = asyncio.run(execute_search_join_with_client(client, _payload(bot_username="jisou"), keyword_text="郑州"))

    assert result["success"] is True
    assert result["page"] == 4
    assert result["searched_pages"] == 4
    assert first_page.clicked == [(1, 0)]
    assert second_page.clicked == [(1, 0)]
    assert third_page.clicked == [(1, 0)]


@pytest.mark.no_postgres
def test_execute_search_join_rejects_unfiltered_jisou_results_when_group_selector_is_missing() -> None:
    result_page = FakeMessage(101, [[FakeButton("📢", data=b"channel-category")]])
    client = FakeSearchJoinClient([FakeMessage(100, []), result_page])

    result = asyncio.run(execute_search_join_with_client(client, _payload(bot_username="jisou"), keyword_text="郑州"))

    assert result["success"] is False
    assert result["error_code"] == "jisou_group_selector_missing"
    assert result["jisou_page_phase"] == "search_category_page"
    selector_page = result["search_protocol_trace"]["selector_page"]
    assert selector_page["button_count"] == 1
    assert selector_page["button_layout"][0]["approved_sample_match"] is True
    assert "text" not in selector_page["button_layout"][0]
    assert result_page.clicked == []
    assert client.joined == []


@pytest.mark.no_postgres
def test_execute_search_join_accepts_jisou_group_results_page_without_category_selector() -> None:
    result_page = FakeMessage(101, [[FakeButton("目标群", url="https://t.me/target_group")]])
    client = FakeSearchJoinClient([FakeMessage(100, []), result_page])

    result = asyncio.run(execute_search_join_with_client(client, _payload(bot_username="jisou"), keyword_text="郑州"))

    assert result["success"] is True
    assert result_page.clicked == [(0, 0)]


@pytest.mark.no_postgres
def test_jisou_bootstrap_skips_start_for_existing_conversation() -> None:
    category_page = FakeMessage(
        101,
        [[FakeButton("👥", data=b"group-category")]],
    )
    result_page = FakeMessage(
        102,
        [[FakeButton("目标群", url="https://t.me/target_group")]],
    )
    client = FakeSearchJoinClient(
        [category_page, result_page],
        history_messages=[FakeMessage(99, [], raw_text="历史会话")],
    )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
        )
    )

    assert result["success"] is True
    assert result["jisou_bootstrap_kind"] == "existing_conversation"
    assert client.sent == [("jisou", "郑州")]


@pytest.mark.no_postgres
def test_jisou_bootstrap_starts_only_first_conversation() -> None:
    category_page = FakeMessage(
        101,
        [[FakeButton("👥", data=b"group-category")]],
    )
    result_page = FakeMessage(
        102,
        [[FakeButton("目标群", url="https://t.me/target_group")]],
    )
    client = FakeSearchJoinClient(
        [FakeMessage(100, [], raw_text="欢迎"), category_page, result_page],
    )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
        )
    )

    assert result["success"] is True
    assert result["jisou_bootstrap_kind"] == "first_conversation"
    assert client.sent == [("jisou", "/start"), ("jisou", "郑州")]


@pytest.mark.no_postgres
def test_execute_search_join_does_not_reset_hot_list_session() -> None:
    hot_list_page = FakeMessage(
        101,
        [[FakeButton("未知入口", data=b"unknown")]],
        raw_text="热搜排行榜",
    )
    client = FakeSearchJoinClient(
        [
            FakeMessage(100, []),
            hot_list_page,
        ]
    )

    result = asyncio.run(execute_search_join_with_client(client, _payload(bot_username="jisou"), keyword_text="郑州"))

    assert result["success"] is False
    assert result["error_code"] == "jisou_hot_list_page"
    assert result["jisou_recovery_kind"] == "not_applicable"
    assert result["reset_executed"] is False
    assert client.sent == [
        ("jisou", "/start"),
        ("jisou", "郑州"),
    ]
    assert hot_list_page.clicked == []


@pytest.mark.no_postgres
def test_execute_search_join_blocks_when_hot_list_reset_still_deviates() -> None:
    hot_list_page = FakeMessage(101, [], raw_text="热搜排行榜")
    deviated_page = FakeMessage(105, [], raw_text="未知页面")
    client = FakeSearchJoinClient(
        [
            FakeMessage(100, []),
            hot_list_page,
            FakeMessage(102, [], raw_text="已取消"),
            FakeMessage(103, [], raw_text="欢迎"),
            deviated_page,
        ]
    )

    result = asyncio.run(execute_search_join_with_client(client, _payload(bot_username="jisou"), keyword_text="郑州"))

    assert result["success"] is False
    assert result["error_code"] == "jisou_hot_list_page"
    assert result["jisou_recovery_kind"] == "not_applicable"
    assert result["reset_executed"] is False
    assert result["jisou_page_phase"] == "hot_list_page"


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
def test_execute_search_join_marks_known_target_when_message_text_matches() -> None:
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
    assert client.joined == []
    assert result["join_status"] == "target_found"
    assert result["target_match_source"] == "message_text"
    assert result["target_line_hash"] == hashlib.sha256("👥郑州平价资源（交流群） @xiaozisk 46k".encode()).hexdigest()
    assert result["target_line_length"] == len("👥郑州平价资源（交流群） @xiaozisk 46k")
    assert "target_line" not in result


@pytest.mark.no_postgres
def test_execute_search_join_uses_visible_exact_title_with_configured_username_on_jisou_page_four() -> None:
    category_page = FakeMessage(101, [[FakeButton("👥", data=b"group-category")]])
    first_page = FakeMessage(102, [[FakeButton("其他群一", url="https://t.me/other_1")], [FakeButton("➡️", data=b"next-1")]])
    second_page = FakeMessage(103, [[FakeButton("其他群二", url="https://t.me/other_2")], [FakeButton("➡️", data=b"next-2")]])
    third_page = FakeMessage(104, [[FakeButton("其他群三", url="https://t.me/other_3")], [FakeButton("➡️", data=b"next-3")]])
    target_page = FakeMessage(
        105,
        [[FakeButton("其他群四", url="https://t.me/other_4")]],
        raw_text="👥 河南郑州学生会 · 公开群",
    )
    client = FakeSearchJoinClient(
        [FakeMessage(100, []), category_page, first_page, second_page, third_page, target_page]
    )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(
                bot_username="jisou",
                target_username="zzxshxc",
                target_title="河南郑州学生会",
                target_peer_id="-1003298633687",
            ),
            keyword_text="郑州",
        )
    )

    assert result["success"] is True
    assert result["page"] == 4
    assert result["target_match_source"] == "message_title_username_verified"
    assert result["target_line_hash"] == hashlib.sha256("👥 河南郑州学生会 · 公开群".encode()).hexdigest()
    assert "target_line" not in result
    assert target_page.clicked == []
    assert client.joined == []


@pytest.mark.no_postgres
def test_execute_search_join_does_not_use_title_prefix_as_target_match() -> None:
    result_page = FakeMessage(
        101,
        [[FakeButton("其他群", url="https://t.me/other_group")]],
        raw_text="👥 河南郑州学生会新生群",
    )
    client = FakeSearchJoinClient([FakeMessage(100, []), result_page])

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(target_username="zzxshxc", target_title="河南郑州学生会"),
            keyword_text="郑州",
        )
    )

    assert result["error_code"] == "target_not_in_results"
    assert client.joined == []


@pytest.mark.no_postgres
def test_execute_search_join_counts_target_click_success_when_account_already_joined() -> None:
    client = FakeSearchJoinClient([], join_error=FakeAlreadyParticipantError())

    result = asyncio.run(ensure_search_join_membership_with_client(client, _payload()))

    assert result["success"] is True
    assert result["join_status"] == "membership_observed"


@pytest.mark.no_postgres
def test_execute_search_join_records_target_match_when_join_request_is_pending() -> None:
    result_page = FakeMessage(101, [], raw_text="👥 河南郑州学生会 · 公开群")
    client = FakeSearchJoinClient(
        [FakeMessage(100, []), result_page],
        join_error=FakeJoinRequestPendingError(),
    )

    source_result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(target_username="zzxshxc", target_title="河南郑州学生会"),
            keyword_text="郑州",
        )
    )
    result = asyncio.run(ensure_search_join_membership_with_client(client, _payload(target_username="zzxshxc", target_title="河南郑州学生会")))

    assert result["success"] is False
    assert result["error_code"] == "join_request_pending"
    assert result["join_status"] == "join_request_pending"
    assert "membership_observed" not in result
    assert source_result["search_end_reason"] == "target_found"
    assert source_result["target_match_source"] == "message_title_username_verified"
    assert source_result["target_line_hash"] == hashlib.sha256("👥 河南郑州学生会 · 公开群".encode()).hexdigest()
    assert "target_line" not in source_result
    assert client.read_targets == []


@pytest.mark.no_postgres
def test_execute_search_join_reports_bot_human_verification() -> None:
    captcha_page = FakeMessage(101, [[FakeButton("42", data=b"answer")]], raw_text="您必须完成人机验证才能继续使用\n请选择计算结果")
    client = FakeSearchJoinClient([FakeMessage(100, []), captcha_page])

    result = asyncio.run(execute_search_join_with_client(client, _payload(), keyword_text="郑州"))

    assert result["success"] is False
    assert result["error_code"] == "bot_human_verification_required"
    assert result["remote_mutation_started"] is False
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
    assert client.joined == []


@pytest.mark.no_postgres
def test_probe_search_join_membership_keeps_waiting_for_approval_visible() -> None:
    client = FakeSearchJoinClient([], membership_probe_error=FakeNotParticipantError())

    result = asyncio.run(probe_search_join_membership_with_client(client, _payload()))

    assert result["success"] is False
    assert result["error_code"] == "membership_not_observed"
    assert result["join_status"] == "membership_pending"


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
        [[FakeButton("其他群", url="https://t.me/other_group")], [FakeButton("⏮️", data=b"previous-page")]],
    )
    client = FakeSearchJoinClient([FakeMessage(100, []), category_page, result_page])

    result = asyncio.run(execute_search_join_with_client(client, _payload(bot_username="jisou"), keyword_text="郑州"))

    assert result["error_code"] == "target_not_in_results"
    trace = result["search_protocol_trace"]
    assert trace["jisou_group_selector"] == {
        "position": 1,
        "text_hash": "d7b93ac850112f54",
        "text_length": 1,
        "approved_sample_match": True,
    }
    assert [item["approved_sample_match"] for item in trace["selector_page"]["button_layout"]] == [True, False]
    assert [item["approved_sample_match"] for item in trace["result_page"]["button_layout"]] == [True, False]
    assert "text" not in trace["jisou_group_selector"]


class _FakeMediaPhoto:
    """PRD §2.19.1: 模拟 telethon MessageMediaPhoto，用于 verification_image_page 检测。"""

    photo = object()


def _verification_image_page(
    *,
    digit_answers: list[str],
    raw_text: str = "人机验证 请计算结果",
    message_id: int = 101,
) -> FakeMessage:
    """构造一个 verification_image_page：含 photo + 人机验证文本 + ≥8 个数字 callback_data 按钮。"""
    buttons = [[FakeButton(text=ans, data=ans.encode()) for ans in digit_answers]]
    return FakeMessage(
        message_id,
        buttons,
        raw_text=raw_text,
        media=_FakeMediaPhoto(),
    )


def _solver_returning(answer: str, confidence: float):
    """构造一个总是返回三路共识决定的 solver callable。"""
    def _solver(_request) -> ImageVerificationDecision:
        return _verification_decision(answer, confidence)
    return _solver


def _verification_decision(
    answer: str,
    confidence: float,
) -> ImageVerificationDecision:
    votes = (
        ImageVerificationVote("model", "accepted", answer, 0.95, True),
        ImageVerificationVote("tesseract", "accepted", answer, 0.80, True),
        ImageVerificationVote("rapidocr", "unsafe", "", 0.0, False),
    )
    return ImageVerificationDecision(answer, confidence, votes)


def _deadline_verification_decision(
    answer: str,
    *,
    deadline_offset: float,
) -> ImageVerificationDecision:
    decision = _verification_decision(answer, 0.95)
    return ImageVerificationDecision(
        answer=decision.answer,
        confidence=decision.confidence,
        votes=decision.votes,
        callback_submit_deadline_monotonic=(
            monotonic() + deadline_offset
        ),
    )


@pytest.mark.no_postgres
def test_jisou_image_verification_succeeds_when_answer_in_button_matrix() -> None:
    """PRD §2.19.2: 验证码识别成功——置信度 ≥0.70 且 answer 在按钮矩阵中，点击后进入 group_result_page。"""
    digit_answers = ["7", "8", "9", "10", "11", "12", "13", "14"]
    verification_page = _verification_image_page(digit_answers=digit_answers)
    # 点击后编辑为 group_result_page（含目标群）
    target_page = FakeMessage(102, [[FakeButton("目标群", url="https://t.me/target_group")]])
    client = FakeSearchJoinClient(
        [FakeMessage(100, []), verification_page],
        edits=[target_page],
    )
    solver = _solver_returning("9", 0.95)

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=solver,
        )
    )

    assert result["success"] is True
    assert result["join_status"] == "target_found"


@pytest.mark.no_postgres
def test_deadline_contract_rereads_same_fingerprint_before_callback() -> None:
    answers = ["7", "8", "9", "10", "11", "12", "13", "14"]
    observed_page = _verification_image_page(digit_answers=answers)
    refreshed_page = _verification_image_page(digit_answers=answers)
    target_page = FakeMessage(
        102,
        [[FakeButton("目标群", url="https://t.me/target_group")]],
    )
    client = FakeSearchJoinClient(
        [FakeMessage(100, []), observed_page],
        edits=[refreshed_page, target_page],
    )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=lambda _request: (
                _deadline_verification_decision("9", deadline_offset=2)
            ),
        )
    )

    assert result["success"] is True
    assert observed_page.clicked == []
    assert refreshed_page.clicked == [(0, 2)]
    audit = result["image_verification_attempts"][0]
    assert audit["message_id"] == "101"
    assert audit["bot_peer_hash"]
    assert audit["message_revision"]
    assert audit["image_hash"]
    assert audit["candidate_hash"]


@pytest.mark.no_postgres
def test_deadline_contract_never_clicks_changed_fingerprint() -> None:
    answers = ["7", "8", "9", "10", "11", "12", "13", "14"]
    observed_page = _verification_image_page(digit_answers=answers)
    changed_page = _verification_image_page(
        digit_answers=answers,
        message_id=102,
    )
    client = FakeSearchJoinClient(
        [FakeMessage(100, []), observed_page],
        edits=[changed_page],
    )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=lambda _request: (
                _deadline_verification_decision("9", deadline_offset=2)
            ),
            image_verification_challenge_limit=1,
        )
    )

    assert result["success"] is False
    assert observed_page.clicked == []
    assert changed_page.clicked == []


@pytest.mark.no_postgres
def test_deadline_contract_never_clicks_after_deadline() -> None:
    answers = ["7", "8", "9", "10", "11", "12", "13", "14"]
    observed_page = _verification_image_page(digit_answers=answers)
    client = FakeSearchJoinClient(
        [FakeMessage(100, []), observed_page],
    )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=lambda _request: (
                _deadline_verification_decision("9", deadline_offset=-1)
            ),
            image_verification_challenge_limit=1,
        )
    )

    assert result["success"] is False
    assert result["remote_mutation_started"] is False
    assert observed_page.clicked == []


@pytest.mark.no_postgres
def test_preflight_rechecks_deadline_after_page_download(monkeypatch) -> None:
    answers = ["7", "8", "9", "10", "11", "12", "13", "14"]
    observed_page = _verification_image_page(digit_answers=answers)
    refreshed_page = _verification_image_page(digit_answers=answers)
    client = FakeSearchJoinClient([], edits=[refreshed_page])
    buttons = search_join_module._parse_buttons(observed_page)
    fingerprint = search_join_module._image_verification_fingerprint(
        observed_page,
        buttons,
        client.download_media_result,
    )
    clock = iter((10.0, 20.0))
    monkeypatch.setattr(
        search_join_module,
        "monotonic",
        lambda: next(clock),
    )

    result = asyncio.run(
        search_join_module._verification_callback_preflight(
            client,
            "jisou",
            observed_page,
            fingerprint,
            15.0,
            protocol_profile=_jisou_protocol_profile(),
        )
    )

    assert result.error is not None
    assert result.error["error_code"] == "jisou_image_verification_required"
    assert result.error["image_verification_reason"] == (
        "verification_deadline_exceeded"
    )
    assert result.error["remote_mutation_started"] is False
    assert refreshed_page.clicked == []


@pytest.mark.no_postgres
def test_callback_deadline_is_checked_again_immediately_before_click(
    monkeypatch,
) -> None:
    answers = ["7", "8", "9", "10", "11", "12", "13", "14"]
    observed_page = _verification_image_page(digit_answers=answers)
    callback_page = _verification_image_page(digit_answers=answers)
    client = FakeSearchJoinClient(
        [FakeMessage(100, []), observed_page],
        edits=[callback_page],
    )
    clock = iter((10.0, 11.0, 12.0, 20.0))
    monkeypatch.setattr(search_join_module, "monotonic", lambda: next(clock))
    decision = ImageVerificationDecision(
        answer="9",
        confidence=0.95,
        votes=_verification_decision("9", 0.95).votes,
        callback_submit_deadline_monotonic=15.0,
    )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=lambda _request: decision,
            image_verification_challenge_limit=1,
        )
    )

    assert result["error_code"] == "jisou_image_verification_required"
    assert result["image_verification_reason"] == "verification_deadline_exceeded"
    assert result["remote_mutation_started"] is False
    assert callback_page.clicked == []


@pytest.mark.no_postgres
def test_callback_response_timeout_is_unknown_after_single_click(
    monkeypatch,
) -> None:
    answers = ["7", "8", "9", "10", "11", "12", "13", "14"]
    observed_page = _verification_image_page(digit_answers=answers)
    callback_page = _verification_image_page(digit_answers=answers)
    client = FakeSearchJoinClient(
        [FakeMessage(100, []), observed_page],
        edits=[callback_page],
    )
    monkeypatch.setattr(
        search_join_module,
        "CALLBACK_PAGE_RESPONSE_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        search_join_module,
        "CALLBACK_PAGE_POLL_INTERVAL_SECONDS",
        0.001,
    )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=lambda _request: (
                _deadline_verification_decision("9", deadline_offset=2)
            ),
            image_verification_challenge_limit=1,
        )
    )

    assert result["error_code"] == "verification_callback_result_unknown"
    assert result["image_verification_status"] == "unknown"
    assert result["callback_mutation_started"] is True
    assert "remote_mutation_started" not in result
    assert len(result["challenge_fingerprint_hash"]) == 64
    assert callback_page.clicked == [(0, 2)]


@pytest.mark.no_postgres
def test_prior_unknown_fingerprint_blocks_solver_and_callback() -> None:
    answers = ["7", "8", "9", "10", "11", "12", "13", "14"]
    observed_page = _verification_image_page(digit_answers=answers)
    client = FakeSearchJoinClient([FakeMessage(100, []), observed_page])
    fingerprint = search_join_module._image_verification_fingerprint(
        observed_page,
        search_join_module._parse_buttons(observed_page),
        client.download_media_result,
    )
    solver_calls = 0

    def solver(_request):
        nonlocal solver_calls
        solver_calls += 1
        return _deadline_verification_decision("9", deadline_offset=2)

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=solver,
            image_verification_callback_unknown_fingerprints=frozenset(
                {fingerprint}
            ),
        )
    )

    assert result["error_code"] == "verification_callback_result_unknown"
    assert result["challenge_fingerprint_hash"] == fingerprint
    assert result["callback_mutation_started"] is True
    assert solver_calls == 0
    assert observed_page.clicked == []


@pytest.mark.no_postgres
def test_unknown_worker_result_rereads_same_challenge_once() -> None:
    answers = ["7", "8", "9", "10", "11", "12", "13", "14"]
    observed_page = _verification_image_page(digit_answers=answers)
    recovery_page = _verification_image_page(digit_answers=answers)
    callback_page = _verification_image_page(digit_answers=answers)
    target_page = FakeMessage(
        102,
        [[FakeButton("目标群", url="https://t.me/target_group")]],
    )
    client = FakeSearchJoinClient(
        [FakeMessage(100, []), observed_page],
        edits=[recovery_page, callback_page, target_page],
    )
    calls = 0

    def solve(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ImageVerificationRuntimeContractError(
                "verification_local_ocr_unknown",
                "worker generation changed",
                callback_submit_deadline_monotonic=monotonic() + 2,
            )
        return _deadline_verification_decision("9", deadline_offset=2)

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=solve,
        )
    )

    assert result["success"] is True
    assert calls == 2
    assert observed_page.clicked == []
    assert recovery_page.clicked == []
    assert callback_page.clicked == [(0, 2)]
    assert client.download_media_calls == 3


@pytest.mark.no_postgres
def test_jisou_string_verification_passes_text_and_alphanumeric_candidates() -> None:
    answers = [
        "A7B9",
        "C3D1",
        "E5F2",
        "G8H4",
        "J6K0",
        "L2M7",
        "N9P3",
        "Q4R8",
    ]
    challenge_text = "人机验证 请选择图片中的字符串"
    verification_page = _verification_image_page(
        digit_answers=answers,
        raw_text=challenge_text,
    )
    target_page = FakeMessage(
        102,
        [[FakeButton("目标群", url="https://t.me/target_group")]],
    )
    client = FakeSearchJoinClient(
        [FakeMessage(100, []), verification_page],
        edits=[target_page],
    )
    requests = []

    def solver(request):
        requests.append(request)
        return _verification_decision("A7B9", 0.95)

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=solver,
        )
    )

    assert result["success"] is True
    assert requests[0].candidate_answers == tuple(answers)
    assert requests[0].challenge_text == challenge_text
    assert verification_page.clicked == [(0, 0)]


@pytest.mark.no_postgres
def test_jisou_selects_group_after_verification_hot_list() -> None:
    verification_page = _verification_image_page(
        digit_answers=["7", "8", "9", "10", "11", "12", "13", "14"]
    )
    hot_list_page = FakeMessage(
        102,
        [[FakeButton("👥", data=b"group-category")]],
        raw_text="热搜排行榜",
    )
    target_page = FakeMessage(
        103,
        [[FakeButton("目标群", url="https://t.me/target_group")]],
    )
    client = FakeSearchJoinClient(
        [verification_page],
        edits=[hot_list_page, target_page],
        history_messages=[FakeMessage(100, [])],
    )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=_solver_returning("9", 0.95),
        )
    )

    assert result["success"] is True
    assert result["join_status"] == "target_found"
    assert result["jisou_post_verification_keyword_replayed"] is False
    assert client.sent == [("jisou", "郑州")]
    assert hot_list_page.clicked == [(0, 0)]


@pytest.mark.no_postgres
def test_jisou_does_not_replay_keyword_when_hot_list_lacks_selector() -> None:
    verification_page = _verification_image_page(
        digit_answers=["7", "8", "9", "10", "11", "12", "13", "14"],
        message_id=101,
    )
    hot_list_page = FakeMessage(102, [], raw_text="热搜排行榜")
    client = FakeSearchJoinClient(
        [verification_page],
        edits=[hot_list_page],
        history_messages=[FakeMessage(100, [])],
    )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=_solver_returning("9", 0.95),
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "jisou_hot_list_page"
    assert result["jisou_post_verification_keyword_replayed"] is False
    assert client.sent == [("jisou", "郑州")]


@pytest.mark.no_postgres
def test_jisou_continues_with_new_challenge_fingerprint_in_same_action() -> None:
    first_verification = _verification_image_page(
        digit_answers=["7", "8", "9", "10", "11", "12", "13", "14"],
        message_id=101,
    )
    second_verification = _verification_image_page(
        digit_answers=["7", "8", "9", "10", "11", "12", "13", "14"],
        message_id=102,
    )
    category_page = FakeMessage(103, [[FakeButton("👥", data=b"group-category")]])
    target_page = FakeMessage(104, [[FakeButton("目标群", url="https://t.me/target_group")]])
    client = FakeSearchJoinClient(
        [first_verification],
        edits=[second_verification, category_page, target_page],
        history_messages=[FakeMessage(100, [])],
    )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=_solver_returning("9", 0.95),
        )
    )

    assert result["success"] is True
    assert result["join_status"] == "target_found"
    assert client.sent == [("jisou", "郑州")]
    assert [
        attempt["status"]
        for attempt in result["image_verification_attempts"]
    ] == [
        "new_challenge_fingerprint",
        "consensus_submitted",
    ]
    assert all(
        len(attempt["votes"]) == 3
        for attempt in result["image_verification_attempts"]
    )


@pytest.mark.no_postgres
def test_jisou_refreshes_keyword_after_no_consensus_then_succeeds() -> None:
    first_verification = _verification_image_page(
        digit_answers=["7", "8", "9", "10", "11", "12", "13", "14"],
        message_id=101,
    )
    second_verification = _verification_image_page(
        digit_answers=["7", "8", "9", "10", "11", "12", "13", "14"],
        message_id=102,
    )
    category_page = FakeMessage(
        103, [[FakeButton("👥", data=b"group-category")]]
    )
    target_page = FakeMessage(
        104, [[FakeButton("目标群", url="https://t.me/target_group")]]
    )
    client = FakeSearchJoinClient(
        [first_verification, second_verification],
        edits=[category_page, target_page],
        history_messages=[FakeMessage(100, [])],
    )
    calls = 0

    def solver(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            votes = (
                ImageVerificationVote("model", "accepted", "7", 0.9, True),
                ImageVerificationVote("rapidocr", "accepted", "8", 0.9, True),
                ImageVerificationVote("ddddocr", "accepted", "9", 0.9, True),
            )
            raise ImageVerificationConsensusUnavailableError(
                "no two sources agree",
                votes,
            )
        return _verification_decision("9", 0.95)

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=solver,
            image_verification_challenge_limit=3,
        )
    )

    assert result["success"] is True
    assert result["join_status"] == "target_found"
    assert client.sent == [("jisou", "郑州"), ("jisou", "郑州")]
    assert result["image_verification_challenge_count"] == 2
    assert result["image_verification_refresh_count"] == 1


@pytest.mark.no_postgres
def test_jisou_verification_prefers_newer_bot_response_over_stale_message_edit() -> None:
    verification_page = _verification_image_page(
        digit_answers=["7", "8", "9", "10", "11", "12", "13", "14"],
        message_id=101,
    )
    stale_verification = _verification_image_page(
        digit_answers=["7", "8", "9", "10", "11", "12", "13", "14"],
        message_id=101,
    )
    category_page = FakeMessage(102, [[FakeButton("👥", data=b"group-category")]])
    target_page = FakeMessage(103, [[FakeButton("目标群", url="https://t.me/target_group")]])
    client = FakeSearchJoinClient(
        [verification_page],
        edits=[stale_verification, target_page],
        latest_messages=[category_page],
        history_messages=[FakeMessage(100, [])],
    )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=_solver_returning("9", 0.95),
        )
    )

    assert result["success"] is True
    assert result["join_status"] == "target_found"


@pytest.mark.no_postgres
def test_jisou_image_verification_accepts_newer_bot_message_after_callback() -> None:
    verification_page = _verification_image_page(
        digit_answers=["7", "8", "9", "10", "11", "12", "13", "14"]
    )
    target_page = FakeMessage(102, [[FakeButton("目标群", url="https://t.me/target_group")]])
    client = FakeSearchJoinClient(
        [FakeMessage(100, []), verification_page],
        latest_messages=[target_page],
    )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=_solver_returning("9", 0.95),
        )
    )

    assert result["success"] is True
    assert result["join_status"] == "target_found"


@pytest.mark.no_postgres
def test_jisou_image_solver_does_not_block_event_loop() -> None:
    digit_answers = ["7", "8", "9", "10", "11", "12", "13", "14"]
    verification_page = _verification_image_page(digit_answers=digit_answers)
    target_page = FakeMessage(102, [[FakeButton("目标群", url="https://t.me/target_group")]])
    client = FakeSearchJoinClient(
        [FakeMessage(100, []), verification_page],
        edits=[target_page],
    )
    solver_started = threading.Event()
    release_solver = threading.Event()

    def solver(_request) -> ImageVerificationDecision | None:
        solver_started.set()
        if not release_solver.wait(timeout=0.2):
            return None
        return _verification_decision("9", 0.95)

    async def execute_with_event_loop_release() -> dict:
        execution = asyncio.create_task(
            execute_search_join_with_client(
                client,
                _payload(bot_username="jisou"),
                keyword_text="郑州",
                image_verification_solver=solver,
            )
        )
        while not solver_started.is_set():
            await asyncio.sleep(0)
        release_solver.set()
        return await execution

    result = asyncio.run(execute_with_event_loop_release())

    assert result["success"] is True
    assert result["join_status"] == "target_found"


@pytest.mark.no_postgres
def test_jisou_image_verification_fails_when_answer_not_in_button_matrix() -> None:
    """PRD §2.19.2 第 3 步 round 7 场景：高置信度但 answer 不在按钮矩阵，禁止点击，写 jisou_image_verification_failed。"""
    digit_answers = ["8", "9", "10", "11", "12", "13", "14", "15"]
    verification_page = _verification_image_page(digit_answers=digit_answers)
    client = FakeSearchJoinClient([FakeMessage(100, []), verification_page])
    solver = _solver_returning("7", 0.95)  # answer=7 不在按钮矩阵中

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=solver,
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "jisou_image_verification_failed"
    assert result["jisou_page_phase"] == "verification_image_page"
    assert result["remote_mutation_started"] is False
    assert verification_page.clicked == []  # 禁止点击


@pytest.mark.no_postgres
def test_jisou_image_verification_fails_when_consensus_budget_exhausted() -> None:
    digit_answers = ["8", "9", "10", "11", "12", "13", "14", "15"]
    verification_page = _verification_image_page(digit_answers=digit_answers)
    client = FakeSearchJoinClient([FakeMessage(100, []), verification_page])
    votes = (
        ImageVerificationVote(
            "model", "low_confidence", "9", 0.50, True
        ),
        ImageVerificationVote(
            "tesseract", "unsafe", "10", 0.80, True
        ),
        ImageVerificationVote(
            "rapidocr", "unsafe", "11", 0.80, True
        ),
    )

    def solver(_request):
        raise ImageVerificationConsensusUnavailableError(
            "three sources did not reach two votes",
            votes,
        )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=solver,
            image_verification_challenge_limit=1,
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "jisou_image_verification_failed"
    assert "challenge budget exhausted" in result["image_verification_detail"]
    assert verification_page.clicked == []


@pytest.mark.no_postgres
def test_jisou_image_verification_none_fails_when_budget_exhausted() -> None:
    digit_answers = ["8", "9", "10", "11", "12", "13", "14", "15"]
    verification_page = _verification_image_page(digit_answers=digit_answers)
    client = FakeSearchJoinClient([FakeMessage(100, []), verification_page])

    call_count = 0

    def _solver(_request) -> ImageVerificationDecision | None:
        nonlocal call_count
        call_count += 1
        return None  # 始终返回空

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=_solver,
            image_verification_challenge_limit=1,
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "jisou_image_verification_failed"
    assert call_count == 1
    assert verification_page.clicked == []


@pytest.mark.no_postgres
def test_jisou_image_verification_stays_required_when_solver_unavailable() -> None:
    digit_answers = ["8", "9", "10", "11", "12", "13", "14", "15"]
    verification_page = _verification_image_page(digit_answers=digit_answers)
    client = FakeSearchJoinClient([FakeMessage(100, []), verification_page])

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=None,
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "jisou_image_verification_required"
    assert result["image_verification_status"] == "required"
    assert result["image_verification_reason"] == "verification_ai_unavailable"
    assert verification_page.clicked == []


@pytest.mark.no_postgres
def test_jisou_image_verification_stays_required_on_provider_transport_error() -> None:
    verification_page = _verification_image_page(
        digit_answers=["8", "9", "10", "11", "12", "13", "14", "15"],
    )
    client = FakeSearchJoinClient([FakeMessage(100, []), verification_page])

    def unavailable_solver(*_args):
        raise ImageVerificationProviderUnavailableError(
            "MiMo(mimo-v2.5): AI provider HTTP 503",
        )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=unavailable_solver,
        )
    )

    assert result["error_code"] == "jisou_image_verification_required"
    assert result["image_verification_reason"] == "verification_ai_unavailable"
    assert "MiMo(mimo-v2.5)" in result["image_verification_detail"]
    assert verification_page.clicked == []


@pytest.mark.no_postgres
def test_jisou_image_verification_keeps_unsafe_provider_diagnostics() -> None:
    verification_page = _verification_image_page(
        digit_answers=["8", "9", "10", "11", "12", "13", "14", "15"],
    )
    client = FakeSearchJoinClient([FakeMessage(100, []), verification_page])

    def unsafe_solver(*_args):
        raise ImageVerificationNoSafeAnswerError(
            "MiMo(mimo-v2.5): confidence=0.95, in_candidates=false",
        )

    result = asyncio.run(
        execute_search_join_with_client(
            client,
            _payload(bot_username="jisou"),
            keyword_text="郑州",
            image_verification_solver=unsafe_solver,
        )
    )

    assert result["error_code"] == "jisou_image_verification_failed"
    assert "MiMo(mimo-v2.5)" in result["image_verification_detail"]
    assert verification_page.clicked == []
