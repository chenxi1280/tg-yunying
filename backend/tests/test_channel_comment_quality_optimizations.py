from datetime import datetime, timezone
import pytest

from app.services.task_center.ai_generator import (
    _clean_generated_content,
    clean_channel_comment_contents,
)
from app.services.task_center.channel_comment_grounding_extractor import (
    extract_grounding_facts,
    SPEECH_ACTS,
)

pytestmark = pytest.mark.no_postgres


def test_clean_generated_content_preserves_leading_numbers() -> None:
    # Numbers as part of sentences must not be stripped
    assert _clean_generated_content("160的身高配100斤确实匀称") == "160的身高配100斤确实匀称"
    assert _clean_generated_content("600/P这个价格管城能安排毒龙？真的吗") == "600/P这个价格管城能安排毒龙？真的吗"
    assert _clean_generated_content("26岁御姐款") == "26岁御姐款"
    assert _clean_generated_content("36B配100斤看着刚刚好") == "36B配100斤看着刚刚好"


def test_clean_generated_content_strips_list_markers() -> None:
    # List enumerations must be cleanly stripped
    assert _clean_generated_content("1. 160的身高配100斤确实匀称") == "160的身高配100斤确实匀称"
    assert _clean_generated_content("1、管城档期稳是真的") == "管城档期稳是真的"
    assert _clean_generated_content("- 情绪价值这块有人翻车过没") == "情绪价值这块有人翻车过没"
    assert _clean_generated_content("① 确实是这样") == "确实是这样"
    assert _clean_generated_content("（1） 暖暖硬件没提包吹") == "暖暖硬件没提包吹"
    assert _clean_generated_content("一、 颜值气质身材俱佳") == "颜值气质身材俱佳"


def test_grounding_extractor_extracts_diverse_aspects_and_interleaves() -> None:
    post_text = (
        "【郑州楼凤阁验证榜】#L604 【花名】#暖暖老师 @yuxuan520999 "
        "【地址】#管城 【标签】#26岁 #御姐 颜值好看气质佳 身高160、体重100、36B "
        "课费 600/P 1100PP 综合评分9分 态度好不催钟 实拍测评"
    )
    facts = extract_grounding_facts(
        post_text,
        datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
        content_route="adult_review",
    )

    evidence = facts["aspect_evidence_json"]
    aspect_codes = {item["aspect_code"] for item in evidence}

    # Verify multiple distinct aspect codes are extracted
    assert "body_feature" in aspect_codes
    assert "appearance_style" in aspect_codes
    assert "location_booking" in aspect_codes
    assert "service_feature" in aspect_codes
    assert "price_cost" in aspect_codes
    assert "score_rating" in aspect_codes

    variants = facts["semantic_variant_units_json"]
    assert len(variants) == len(evidence) * len(SPEECH_ACTS)

    # Verify that consecutive variants have interleaved (different) aspect codes
    first_four_aspects = [v["aspect_code"] for v in variants[:min(4, len(variants))]]
    assert len(set(first_four_aspects)) >= 3, f"First 4 variants should be diverse: {first_four_aspects}"


def test_clean_channel_comment_contents_caps_repetitive_feature_tokens() -> None:
    # Comments that repeatedly mention the same specific numeric / body feature like '100斤' must be capped
    incoming_comments = [
        "御姐风配100斤确实少见 写真实点还是别太离谱就行",
        "确实 御姐款一直这调调",
        "B配100斤确实少见 骨架应该不大 肉感主要看哪里长？",
        "B杯小归小 36B配100斤看着刚刚好 别想太瘦",  # 3rd time 100斤 -> Should be rejected
        "160的身高配100斤确实匀称 难怪评9",           # 4th time 100斤 -> Should be rejected
        "管城档期稳是真的 这家综合9分算实在",
        "高端的一般都能加项目吧 课表是不是还能再议？",
    ]

    accepted = clean_channel_comment_contents(incoming_comments)
    # Verify that '100斤' appears at most 2 times across accepted comments
    count_100jin = sum(1 for c in accepted if "100斤" in c)
    assert count_100jin <= 2, f"'100斤' should appear at most 2 times, got {count_100jin}"
    assert "管城档期稳是真的 这家综合9分算实在" in accepted
    assert "高端的一般都能加项目吧 课表是不是还能再议？" in accepted


def test_clean_channel_comment_contents_supports_three_tier_length_distribution() -> None:
    # Verify that ultra-short comments (e.g. 爽翻天, 好便宜, 真顶, 插眼) and long detailed comments are accepted
    incoming_comments = [
        "爽翻天",                                              # Ultra-short (3 chars)
        "好便宜",                                              # Ultra-short (3 chars)
        "真顶",                                                # Ultra-short (2 chars)
        "老哥稳",                                              # Ultra-short (3 chars)
        "这照片修得亲妈都不认识了吧哈哈",                         # Medium (15 chars)
        "600这年头在管城算良心了",                               # Medium (12 chars)
        "御姐好啊我就吃这套 看着挺顶",                            # Medium (13 chars)
        "看了半天不知道催不催钟，上周去别的地方被催成狗，要是真能有9分下周发工资去探探",  # Long (36 chars)
    ]

    accepted = clean_channel_comment_contents(incoming_comments)
    assert "爽翻天" in accepted
    assert "好便宜" in accepted
    assert "真顶" in accepted
    assert "老哥稳" in accepted
    assert "这照片修得亲妈都不认识了吧哈哈" in accepted
    assert "看了半天不知道催不催钟 上周去别的地方被催成狗 要是真能有9分下周发工资去探探" in accepted

    # Check length tier proportions
    ultra_short = [c for c in accepted if len(c) <= 6]
    medium = [c for c in accepted if 7 <= len(c) <= 17]
    long_comments = [c for c in accepted if len(c) >= 18]

    assert len(ultra_short) >= 3
    assert len(medium) >= 3
    assert len(long_comments) >= 1


