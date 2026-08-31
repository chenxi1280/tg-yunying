import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.task_center.ai_generator import (
    _extract_channel_post_aspects,
    _format_post_aspects_prompt,
    _is_adult_channel_context,
    generate_channel_comments,
)


pytestmark = pytest.mark.no_postgres


def test_extract_channel_post_aspects_teacher_name():
    # Case 1: Bracketed city with teacher name
    p1 = "【广州天河】糖糖老师 170/48/D杯 极品白幼瘦黑丝 刚到天河已开课 主打柔情水疗 配合度极高"
    res1 = _extract_channel_post_aspects(p1, "广州同城推荐")
    assert res1["teacher_name"] == "糖糖老师"
    aspect_codes = {a["code"] for a in res1["aspects"]}
    assert "visual_body" in aspect_codes
    assert "outfit_style" in aspect_codes
    assert "service_exp" in aspect_codes
    assert "location_booking" in aspect_codes

    # Case 2: Bracketed city with plain name
    p2 = "【深圳南山】晴天 172/49/D 极品御姐 黑丝大长腿 南山公寓开课 主打水疗漫游"
    res2 = _extract_channel_post_aspects(p2, "深圳同城")
    assert res2["teacher_name"] == "晴天"

    # Case 3: Explicit teacher name in sentence
    p3 = "小可老师今日已开课，165/45/C杯，素颜清纯，不催钟配合好，天河附近"
    res3 = _extract_channel_post_aspects(p3, "频道")
    assert res3["teacher_name"] == "小可老师"
    aspect_codes3 = {a["code"] for a in res3["aspects"]}
    assert "authenticity" in aspect_codes3
    assert "service_exp" in aspect_codes3


def test_format_post_aspects_prompt_dispersion_and_name():
    raw = "【广州天河】糖糖老师 170/48/D杯 极品白幼瘦黑丝 刚到天河已开课 主打柔情水疗 配合度极高"
    aspects = _extract_channel_post_aspects(raw, "频道")

    prompt_slot0 = _format_post_aspects_prompt(aspects, slot_ordinal=0, adult_context=True)
    assert "糖糖老师" in prompt_slot0
    assert "本条指定切入方向" in prompt_slot0

    prompt_slot1 = _format_post_aspects_prompt(aspects, slot_ordinal=1, adult_context=True)
    assert "糖糖老师" in prompt_slot1

    prompt_slot2 = _format_post_aspects_prompt(aspects, slot_ordinal=2, adult_context=True)
    assert "糖糖老师" in prompt_slot2

    # Different slots should assign different primary directions
    assert prompt_slot0 != prompt_slot1 or prompt_slot1 != prompt_slot2


def test_is_adult_channel_context_detection():
    # 1. Explicit general route -> False
    assert not _is_adult_channel_context({"content_route": "general"}, "频道", "小可老师黑丝大长腿")

    # 2. Explicit adult config -> True
    assert _is_adult_channel_context({"adult_prompt_enabled": True}, "频道", "普通消息")

    # 3. 正文只能收窄显式路由，不能把无路由任务提升为成人场景
    assert not _is_adult_channel_context({}, "频道", "【广州天河】糖糖老师 170/48/D杯 黑丝大长腿 主打水疗")
    assert not _is_adult_channel_context({}, "频道", "新人开课，水疗漫游不催钟")

    # 4. Pure general content -> False
    assert not _is_adult_channel_context({}, "上海生活日常分享", "今天天气晴朗，大家早安")


def test_generate_channel_comments_includes_aspects_and_teacher(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    captured_requirements = []

    def mock_retry(session, tenant_id, config, *, topic, requirements, count, purpose, target_label, message_content="", local_city=None):
        captured_requirements.append(requirements)
        return ["糖糖老师这大长腿黑丝看着真顶"], 10

    monkeypatch.setattr(
        "app.services.task_center.ai_generator._generate_channel_contents_with_retry",
        mock_retry,
    )

    msg = "【广州天河】糖糖老师 170/48/D杯 极品白幼瘦黑丝 刚到天河已开课 主打柔情水疗 配合度极高"
    with Session(engine) as session:
        contents, tokens = generate_channel_comments(
            session,
            tenant_id=1,
            config={"_comment_slot_ordinal": 0},
            count=1,
            message_content=msg,
            target_label="广州频道",
        )
        assert len(contents) == 1
        assert "糖糖老师" in contents[0]
        assert len(captured_requirements) == 1
        req = captured_requirements[0]
        assert "糖糖老师" in req
        assert "本条指定切入方向" in req
        assert "黑丝" in req or "170" in req or "水疗" in req


def test_active_dynamic_aspect_extraction():
    msg = (
        "【深圳南山】豆豆老师 168/48/D #护士COS #极品御姐\n"
        "主打：独家精油水疗与双人漫游\n"
        "环境：独栋私密海景房带停车位\n"
        "优惠：早鸟特惠立减200\n"
        "今日在南山开课，配合度高不催钟！"
    )
    aspects = _extract_channel_post_aspects(msg, "深圳频道")
    assert aspects["teacher_name"] == "豆豆老师"
    aspect_codes = {a["code"] for a in aspects["aspects"]}
    assert "service_feature" in aspect_codes
    assert "env_feature" in aspect_codes
    assert "promo_feature" in aspect_codes
    assert "hashtags" in aspect_codes

    service_aspect = next(a for a in aspects["aspects"] if a["code"] == "service_feature")
    assert any("独家精油水疗" in m for m in service_aspect["matches"])

    env_aspect = next(a for a in aspects["aspects"] if a["code"] == "env_feature")
    assert any("独栋私密海景房" in m for m in env_aspect["matches"])

    prompt = _format_post_aspects_prompt(aspects, slot_ordinal=0, adult_context=True)
    assert "豆豆老师" in prompt
    assert "本条指定切入方向" in prompt
    assert "Speech Act" in prompt


def test_speech_act_dispersion_and_experience_rejection():
    from app.services.task_center.ai_generator import _looks_like_bad_channel_comment

    msg = "【深圳南山】豆豆老师 168/48/D"
    aspects = _extract_channel_post_aspects(msg, "深圳频道")

    p0 = _format_post_aspects_prompt(aspects, slot_ordinal=0, adult_context=True)
    assert "随性反应" in p0

    p1 = _format_post_aspects_prompt(aspects, slot_ordinal=1, adult_context=True)
    assert "具体问题" in p1

    p2 = _format_post_aspects_prompt(aspects, slot_ordinal=2, adult_context=True)
    assert "谨慎求证" in p2

    p3 = _format_post_aspects_prompt(aspects, slot_ordinal=3, adult_context=True)
    assert "极短附和" in p3

    assert _format_post_aspects_prompt(
        {"teacher_name": "", "aspects": []},
        slot_ordinal=0,
        adult_context=True,
    ) == ""

    # Experience rejection
    assert _looks_like_bad_channel_comment("我去过这个地方真不错")
    assert _looks_like_bad_channel_comment("我上次去找过她态度挺好")
    assert _looks_like_bad_channel_comment("亲测过手法专业")
    assert _looks_like_bad_channel_comment("昨晚试了配合度高")
    assert not _looks_like_bad_channel_comment("豆豆老师这大长腿看着真顶")
