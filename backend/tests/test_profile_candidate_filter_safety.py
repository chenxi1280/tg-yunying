from __future__ import annotations

import pytest

from scripts.profile_candidate_filter import (
    ProfileFilter,
    unique_display_name_from_candidate,
)

pytestmark = pytest.mark.no_postgres


def test_forbidden_words_rejection_in_name_and_bio():
    filter_obj = ProfileFilter(
        our_account_ids={"1001"},
        our_usernames={"ourbot"},
        our_display_names={"老司机"},
        task_discussion_teachers=set(),
        forbidden_words={"禁用词", "代充", "特价"},
    )
    # 1. 禁用词在 display_name 中必须被拦截
    res1 = filter_obj.filter_candidate(user_id="2001", display_name="禁用词老张")
    assert not res1.is_valid
    assert res1.rejection_reason == "forbidden_word_in_name"

    res2 = filter_obj.filter_candidate(user_id="2002", display_name="特价好车")
    assert not res2.is_valid
    assert res2.rejection_reason == "forbidden_word_in_name"

    # 2. 禁用词在 bio 中必须被拦截
    res3 = filter_obj.filter_candidate(user_id="2003", display_name="正常老哥", bio="提供代充服务")
    assert not res3.is_valid
    assert res3.rejection_reason == "forbidden_word_in_bio"

    # 3. 合法真实昵称必须通过
    res4 = filter_obj.filter_candidate(user_id="2004", display_name="成都小李", bio="偶尔上线看看。")
    assert res4.is_valid
    assert res4.rejection_reason == ""


def test_our_ai_account_by_tg_user_id():
    filter_obj = ProfileFilter(
        our_account_ids={"778899"},
        our_usernames={"bot_username"},
        our_display_names={"测试账号"},
        task_discussion_teachers=set(),
    )
    # Telegram sender_peer_id 匹配到我们的 tg_user_id
    res = filter_obj.filter_candidate(user_id="778899", display_name="普通群友")
    assert not res.is_valid
    assert res.rejection_reason == "is_our_ai_account (by ID)"


def test_unique_display_name_strictly_complies_with_prd():
    used_keys = {"老张", "小老张", "老老张", "阿老张", "大老张", "木老张"}
    forbidden = {"禁用词"}

    # 1. 验证生成变体不含数字尾巴，不含 '同学' / '酱'，且 100% 唯一
    disp_name, f_name, l_name = unique_display_name_from_candidate(
        base_name="老张",
        used_keys=used_keys,
        seed_idx=1,
        forbidden_words=forbidden,
    )
    assert disp_name not in {"老张", "老张_10", "老张_100", "老张同学"}
    assert not any(char.isdigit() for char in disp_name)
    assert "同学" not in disp_name
    assert "酱" not in disp_name
    assert ProfileFilter.normalize_name(disp_name) in used_keys

    # 2. 验证变体包含禁用词时被跳过
    used_keys2 = {"小王"}
    disp_name2, _, _ = unique_display_name_from_candidate(
        base_name="小王",
        used_keys=used_keys2,
        seed_idx=0,
        forbidden_words={"小王呀"},
    )
    assert disp_name2 != "小王呀"


def test_unique_display_name_exhaustion_is_explicit():
    base = "这是一个非常非常非常非常非常非常非常长的昵称"

    with pytest.raises(ValueError, match="unique_display_name_exhausted"):
        unique_display_name_from_candidate(
            base_name=base,
            used_keys=set(),
            seed_idx=0,
            forbidden_words={"这是"},
        )


if __name__ == "__main__":
    test_forbidden_words_rejection_in_name_and_bio()
    test_our_ai_account_by_tg_user_id()
    test_unique_display_name_strictly_complies_with_prd()
    print("ALL SAFETY & DIVERSITY PROFILE TESTS PASSED SUCCESSFULLY!")
