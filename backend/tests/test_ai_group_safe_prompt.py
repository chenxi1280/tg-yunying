from __future__ import annotations

import json

import pytest

from app.services.task_center.ai_group_prompt import (
    DRAFT_KEYS,
    build_group_prompt,
    contains_disallowed_group_content,
    sanitize_group_messages,
)


pytestmark = pytest.mark.no_postgres


def _config() -> dict:
    return {
        "adult_prompt_enabled": True,
        "account_personas": {"11": "普通成年群友"},
        "account_profiles": {"11": "自然直接；价格面付；只接安全话题"},
        "active_topic_direction": {"title": "成年人的穿搭讨论", "description": "价格可约"},
        "active_teacher_target": {"name": "这位成年老师", "description": "身材曲线很好看"},
        "generation_slots": [{"sequence_index": 1, "slot_id": "slot-1", "account_id": 11, "act_type": "short_react"}],
    }


def test_sanitizes_transaction_and_age_risk_but_keeps_adult_appearance():
    messages = sanitize_group_messages(
        [
            "这位成年老师身材曲线很好看，多少钱能安排",
            "这位成年老师腿又长又白",
            "黑丝和高跟鞋很搭",
            "这位老师好嫩像学生妹",
            "私聊我发定位",
        ]
    )

    assert messages == [
        "这位成年老师身材曲线很好看",
        "这位成年老师腿又长又白",
        "黑丝和高跟鞋很搭",
    ]


def test_keeps_normal_interest_group_context_without_topic_allowlist():
    messages = sanitize_group_messages([
        "今天聊聊摄影构图和光线",
        "周末徒步路线有人走过吗",
        "多少钱 私聊安排 酒店见",
    ])

    assert messages == ["今天聊聊摄影构图和光线", "周末徒步路线有人走过吗"]


def test_filters_bot_junk_and_game_spam():
    messages = sanitize_group_messages([
        "学生会助手: 露露，您需要关注我们的频道才能发言。",
        "脆脆鲨邀请成功！获得：30 积分",
        "还有挖宝吗",
        "这位老师开课了吗，水头怎么样",
        "昨晚去交了作业，配合度不错",
    ])

    assert messages == ["这位老师开课了吗", "水头怎么样", "昨晚去交了作业", "配合度不错"]


def test_builds_adult_prompt_with_sanitized_chinese_data():
    bundle = build_group_prompt(
        _config(),
        target_label="天津上牌资源群",
        history="真人用户: 这位成年老师气质挺撩人\n广告号: 价格便宜 私聊安排",
        count=1,
    )

    assert "Telegram 同城成人娱乐" in bundle.system_prompt
    assert "老师/课代表" in bundle.system_prompt
    assert "one JSON object only" in bundle.system_prompt
    assert bundle.context_source == "safe_context"
    assert "气质挺撩人" in bundle.user_prompt
    assert "私聊安排" not in bundle.user_prompt


def test_city_school_and_weak_business_words_do_not_authorize_adult_prompt():
    config = {key: value for key, value in _config().items() if key != "adult_prompt_enabled"}

    for label, history in (
        ("郑州摄影交流群", "周末聊摄影构图"),
        ("成都大学校友群", "老师今晚开课吗"),
        ("天津音乐交流群", "周末聊音乐"),
        ("老师避坑交流群", "老哥们去过没"),
    ):
        bundle = build_group_prompt(config, target_label=label, history=history, count=1)
        assert "Generate Chinese community replies" in bundle.system_prompt
        assert "Telegram 同城成人娱乐" not in bundle.system_prompt


def test_explicit_current_route_authorizes_adult_prompt():
    config = {
        "ai_content_route_v2_enabled": True,
        "content_route": "adult_service",
        "ai_content_allowed_routes": ["general", "adult_service"],
    }
    bundle = build_group_prompt(config, target_label="普通名称群", history="已有成年服务语境", count=1)
    assert "Telegram 同城成人娱乐" in bundle.system_prompt

    config["content_route"] = "general"
    bundle = build_group_prompt(config, target_label="郑州大学", history="老师今晚开课吗", count=1)
    assert "Generate Chinese community replies" in bundle.system_prompt


def test_contact_four_categories_all_forbidden():
    from app.services.task_center.ai_generator import (
        clean_channel_comment_contents,
        clean_group_chat_contents,
    )

    probe_samples = [
        # 1. 国际与国内手机号
        "+1 202-555-0123",
        "13800138000",
        "手机号 13812345678",
        "电话 15912345678",
        "拨打 18800001111",
        "联系电话 139-1234-5678",
        # 2. QQ类各种变体
        "加qq联系我",
        "留个QQ",
        "企鹅联系",
        "加QQ 12345678",
        "企鹅: 987654321",
        "扣扣: 10001",
        "QQ号: 88888",
        # 3. TG/电报/飞机类变体
        "飞机号 abc_123",
        "电报号 abc_123",
        "关注 @my_channel",
        "私聊发你",
        "私信发定位",
        # 4. URL/裸域名/协议
        "example.com/x",
        "example.ai/x",
        "example.dev/x",
        "bit.ly/abc",
        "ftp://example.com/x",
        "https://t.me/joinchat",
        "www.example.com",
        # 5. 微信类
        "加微信私聊",
        "微信号 my_wechat",
        "加v看图",
        "v同步 123",
        "vx: test",
        "企微联系",
        "号码 (202) 555-0123",
    ]

    for t in probe_samples:
        assert contains_disallowed_group_content(t), f"contains_disallowed_group_content failed for: {t}"
        assert clean_group_chat_contents([t]) == [], f"clean_group_chat_contents failed for: {t}"
        assert clean_channel_comment_contents([t]) == [], f"clean_channel_comment_contents failed for: {t}"


def test_contact_gate_keeps_non_contact_qq_words_and_business_facts():
    from app.services.task_center.ai_generator import clean_channel_comment_contents

    safe_samples = [
        "今天去动物园看企鹅",
        "这个动画角色叫扣扣",
        "订单号 20260827123",
        "价格有变吗",
        "河东区这个位置方便吗",
    ]

    assert clean_channel_comment_contents(safe_samples) == safe_samples


def test_channel_comment_routing_and_generic_landmarks():
    from app.services.task_center.ai_generator import (
        _channel_comment_cross_city_leak,
        _channel_comment_system_prompt,
        _is_adult_channel_context,
        _sanitize_channel_message_content,
    )

    # City and weak words cannot override the explicit general route.
    gen_config = {"content_route": "general"}
    assert not _is_adult_channel_context(gen_config, "上海生活日常分享", "今天天气不错")
    prompt = _channel_comment_system_prompt(gen_config, "上海生活日常分享", "今天天气不错")
    assert "真实订阅读者" in prompt
    assert "男客老司机" not in prompt

    assert not _is_adult_channel_context(gen_config, "郑州生活日常分享", "今天天气不错")
    assert not _is_adult_channel_context(gen_config, "频道", "这位新开课老师身材水头不错")
    raw_adult_context = "所在位置：河东区；服务项目：陪洗，无套口，制服"
    assert "河东区" not in _sanitize_channel_message_content(raw_adult_context, allow_adult_context=False)
    assert "陪洗" not in _sanitize_channel_message_content(raw_adult_context, allow_adult_context=False)

    # Explicit config also triggers adult context
    adult_config = {"adult_prompt_enabled": True}
    assert _is_adult_channel_context(adult_config, "频道", "普通频道消息")
    assert _sanitize_channel_message_content(raw_adult_context, allow_adult_context=True) == raw_adult_context
    adult_prompt = _channel_comment_system_prompt(adult_config, "频道", "普通频道消息")
    assert "真实活跃的群友老哥" in adult_prompt or "真实男客" in adult_prompt

    # Generic landmarks (高新/经开/新区/大学城) must NOT be considered cross-city leaks
    assert not _channel_comment_cross_city_leak("高新区这边新开的怎么样", "郑州")
    assert not _channel_comment_cross_city_leak("高新区这边新开的怎么样", "成都")
    assert not _channel_comment_cross_city_leak("经开区环境挺不错的", "西安")
    assert not _channel_comment_cross_city_leak("大学城附近有没有好玩的", "天津")

    # True foreign city landmark leak MUST be detected
    assert _channel_comment_cross_city_leak("太古里那边去过吗", "郑州")
    assert _channel_comment_cross_city_leak("南稍门那边水头咋样", "成都")


def test_channel_comment_retry_preserves_facts_and_adult_prompt():
    from app.services.task_center.ai_generator import (
        _channel_comment_attempt_requirements,
        _channel_comment_attempt_system_prompt,
        _channel_comment_attempt_topic,
    )

    config = {"adult_prompt_enabled": True}
    orig_req = "频道消息：这位老师身材很好，开课水头怎么样"
    topic = "频道评论"

    # Retries must preserve topic and original factual context
    assert _channel_comment_attempt_topic(topic, 1, config=config, message_content="老师开课") == "频道评论"
    req_1 = _channel_comment_attempt_requirements(orig_req, 1, config=config, message_content="老师开课")
    assert "这位老师身材很好，开课水头怎么样" in req_1
    assert "重试生成要求" in req_1

    # The retry maintains consistent system prompt with adult role
    sys_prompt = _channel_comment_attempt_system_prompt(1, config=config, target_label="测试", message_content="老师开课")
    assert "真实活跃的群友老哥" in sys_prompt or "真实男客" in sys_prompt
    assert "中性、礼貌" not in sys_prompt

    plain_req = _channel_comment_attempt_requirements(
        "频道消息：今天营业时间有调整",
        1,
        config={},
        target_label="成都怡红院",
        message_content="今天营业时间有调整",
    )
    assert "围绕原帖已有事实" in plain_req
    assert "身材、照片真实度" not in plain_req


def test_generic_warmup_has_no_unsafe_dynamic_text():
    bundle = build_group_prompt(
        _config(),
        target_label="交易资源群",
        history="多少钱 私聊安排 酒店见",
        count=1,
    )

    assert bundle.context_source == "generic_warmup"
    assert bundle.sanitized_context == ()
    assert all(
        phrase in bundle.system_prompt
        for phrase in ("签到", "打卡", "积分", "努力搬砖", "喝咖啡", "犯困", "吃红烧肉")
    )
    assert "私聊安排" not in bundle.user_prompt


def test_output_contract_has_exact_keys_for_each_requested_draft():
    bundle = build_group_prompt(_config(), target_label="普通交流群", history="今天有人签到吗", count=2)
    contract = bundle.output_contract

    assert set(contract) == {"decision", "context_source", "drafts"}
    assert len(contract["drafts"]) == 2
    assert all(set(draft) == DRAFT_KEYS for draft in contract["drafts"])
    assert all("check_in" not in draft["intent"] for draft in contract["drafts"])
    assert contract["drafts"][0]["slot_id"] == "slot-1"
    json.dumps(contract, ensure_ascii=False)


def test_reply_target_is_sanitized_before_prompting():
    bundle = build_group_prompt(
        _config(),
        target_label="普通交流群",
        history="今天有人开课吗",
        count=1,
        reply_targets=[{"author": "@contact", "preview": "老师高跟鞋好看 微信联系", "source": "私聊"}],
    )

    serialized = json.dumps(bundle.input_payload, ensure_ascii=False)
    assert "高跟鞋好看" in serialized
    assert "微信联系" not in serialized
    assert "@contact" not in serialized
    assert "私聊" not in serialized
