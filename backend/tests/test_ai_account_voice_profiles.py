from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountStatus, AiAccountGroupStanceMemory, AiAccountVoiceProfile, AuditLog, TgAccount
from app.services.task_center import account_stance_memory, account_voice_profile_cache
from app.services.task_center.account_voice_profiles import (
    VOICE_PROFILE_INITIAL_MAX_TOKENS,
    VOICE_PROFILE_RETRY_MAX_TOKENS,
    VOICE_PROFILE_BATCH_SIZE,
    _generate_voice_profile_payloads,
    _parse_voice_profile_payloads,
    batch_rebuild_voice_profiles,
    list_voice_profiles,
    patch_voice_profile,
    rebuild_voice_profile,
    ensure_voice_profiles_for_accounts,
    group_stance_summaries,
    upsert_group_stance_memory,
    voice_profile_prompt_details,
    voice_profile_prompt_summaries,
)
from app.services.task_center.account_voice_profile_versions import (
    list_voice_profile_audits,
    list_voice_profile_versions,
    rollback_voice_profile,
)
from app.services.task_center.account_voice_profile_bulk import batch_update_voice_profile_status


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _account(session: Session, account_id: int, name: str, username: str = "") -> None:
    session.add(
        TgAccount(
            id=account_id,
            tenant_id=1,
            display_name=name,
            username=username,
            phone_masked=f"138****{account_id}",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext="session",
        )
    )
    session.commit()


def _profile(account_id: int, summary: str, *, version: int = 1, status: str = "active") -> AiAccountVoiceProfile:
    return AiAccountVoiceProfile(
        tenant_id=1,
        account_id=account_id,
        version=version,
        short_prompt_summary=summary,
        sentence_length="短句",
        interaction_habits=["爱追问"],
        tone_strength="轻松",
        lexical_preferences=["我看看"],
        emoji_policy="少用",
        forbidden_expressions=["确实不错"],
        status=status,
        quality_status="active",
    )


class FakeStanceRedis:
    def __init__(self, values: list[str | None] | None = None) -> None:
        self.values = values or []
        self.mget_calls: list[list[str]] = []
        self.setex_calls: list[tuple[str, int, str]] = []

    def mget(self, keys):  # noqa: ANN001
        self.mget_calls.append([str(key) for key in keys])
        return self.values[: len(keys)]

    def setex(self, key, ttl, value):  # noqa: ANN001
        self.setex_calls.append((str(key), int(ttl), str(value)))
        return True


class FakeVoiceProfileRedis(FakeStanceRedis):
    def __init__(self, values: list[str | None] | None = None) -> None:
        super().__init__(values)
        self.delete_calls: list[str] = []

    def delete(self, key):  # noqa: ANN001
        self.delete_calls.append(str(key))
        return 1


def _enable_voice_profile_redis(monkeypatch, fake_redis: FakeVoiceProfileRedis) -> None:
    monkeypatch.setattr(
        account_voice_profile_cache,
        "get_settings",
        lambda: SimpleNamespace(queue_backend="redis", redis_url="redis://cache"),
    )
    monkeypatch.setattr(account_voice_profile_cache, "_redis_client", lambda _url: fake_redis)


def test_voice_profile_prompt_details_backfills_redis_cache(monkeypatch):
    fake_redis = FakeVoiceProfileRedis([None])
    _enable_voice_profile_redis(monkeypatch, fake_redis)
    with _session() as session:
        _account(session, 101, "花花号")
        session.add(_profile(101, "青年短句，先问价格再看反馈", version=3))
        session.commit()

        details = voice_profile_prompt_details(session, tenant_id=1, account_ids=[101])

        assert details == {101: {"version": 3, "summary": "青年短句，先问价格再看反馈"}}
        assert fake_redis.mget_calls == [["ai_group:voice_profile:1:101"]]
        assert fake_redis.setex_calls
        assert json.loads(fake_redis.setex_calls[0][2]) == {
            "account_id": 101,
            "version": 3,
            "summary": "青年短句，先问价格再看反馈",
        }


def test_voice_profile_patch_refreshes_redis_cache(monkeypatch):
    fake_redis = FakeVoiceProfileRedis()
    _enable_voice_profile_redis(monkeypatch, fake_redis)
    with _session() as session:
        _account(session, 101, "花花号")
        session.add(_profile(101, "青年短句，先问价格再看反馈", version=1))
        session.commit()

        patch_voice_profile(
            session,
            tenant_id=1,
            account_id=101,
            patch={"short_prompt_summary": "中年中句，先看反馈再轻吐槽"},
            actor="tester",
        )

        assert fake_redis.setex_calls
        refreshed = json.loads(fake_redis.setex_calls[-1][2])
        assert refreshed == {
            "account_id": 101,
            "version": 2,
            "summary": "中年中句，先看反馈再轻吐槽",
        }


def test_missing_voice_profile_requires_explicit_ai_generator():
    with _session() as session:
        with pytest.raises(RuntimeError, match="voice profile generator is required"):
            ensure_voice_profiles_for_accounts(session, tenant_id=1, account_ids=[101], generator=None)


def test_ensure_voice_profiles_uses_batch_generator_and_rejects_generic_summary():
    def generator(account_ids: list[int]) -> list[dict]:
        assert account_ids == [101, 102]
        return [
            {
                "account_id": 101,
                "age_band": "青年",
                "sentence_length": "短句",
                "interaction_habits": ["爱追问价格", "少发表情"],
                "tone_strength": "轻松",
                "lexical_preferences": ["还行", "我看看"],
                "emoji_policy": "少用",
                "forbidden_expressions": ["确实不错"],
                "short_prompt_summary": "青年短句，爱追问价格，少表情，偶尔说我看看",
            },
            {
                "account_id": 102,
                "age_band": "中年",
                "sentence_length": "中句",
                "interaction_habits": ["爱补经历", "偶尔轻吐槽"],
                "tone_strength": "谨慎",
                "lexical_preferences": ["稳一点", "别急"],
                "emoji_policy": "不用表情",
                "forbidden_expressions": ["感觉挺靠谱"],
                "short_prompt_summary": "中年中句，谨慎补经历，偶尔轻吐槽，不用表情",
            },
        ]

    with _session() as session:
        created = ensure_voice_profiles_for_accounts(session, tenant_id=1, account_ids=[101, 102], generator=generator)
        session.commit()

        rows = list(session.scalars(select(AiAccountVoiceProfile).order_by(AiAccountVoiceProfile.account_id)))
        assert created == 2
        assert [row.account_id for row in rows] == [101, 102]
        assert rows[0].version == 1
        assert rows[0].quality_status == "active"
        assert rows[0].similarity_score is not None
        assert rows[1].similarity_score is not None
        assert voice_profile_prompt_summaries(session, tenant_id=1, account_ids=[101, 102]) == {
            101: "青年短句，爱追问价格，少表情，偶尔说我看看",
            102: "中年中句，谨慎补经历，偶尔轻吐槽，不用表情",
        }


def test_ensure_voice_profiles_splits_large_generation_batches():
    account_ids = list(range(1000, 1000 + VOICE_PROFILE_BATCH_SIZE + 2))
    calls: list[list[int]] = []
    habits = ["追问价格", "补充体验", "吐槽排队", "爱问位置", "只接半句", "喜欢附和", "先观望", "爱问照片"]
    words = ["我看看", "别急", "稳点", "有谱", "空了说", "别跑空", "试过", "还行"]
    summaries = {
        account_id: "".join(chr(0x4E00 + ((account_id * 37 + index * 97) % 1800)) for index in range(18))
        for account_id in account_ids
    }

    def generator(ids: list[int]) -> list[dict]:
        calls.append(ids)
        return [
            {
                "account_id": account_id,
                "age_band": "青年" if account_id % 2 else "中年",
                "sentence_length": "短句" if account_id % 3 else "中句",
                "interaction_habits": [habits[account_id % len(habits)], "少发表情"],
                "tone_strength": "轻松" if account_id % 5 else "谨慎",
                "lexical_preferences": [words[account_id % len(words)], str(account_id)],
                "emoji_policy": "少用" if account_id % 7 else "不用表情",
                "forbidden_expressions": ["确实不错"],
                "short_prompt_summary": summaries[account_id],
            }
            for account_id in ids
        ]

    with _session() as session:
        for account_id in account_ids:
            _account(session, account_id, f"账号{account_id}")

        created = ensure_voice_profiles_for_accounts(session, tenant_id=1, account_ids=account_ids, generator=generator)
        session.commit()

        assert created == len(account_ids)
        assert [len(call) for call in calls] == [VOICE_PROFILE_BATCH_SIZE, 2]
        assert session.scalar(select(func.count(AiAccountVoiceProfile.id))) == len(account_ids)


def test_ensure_voice_profiles_rejects_vague_summary():
    def generator(_account_ids: list[int]) -> list[dict]:
        return [{"account_id": 101, "short_prompt_summary": "自然、随意、真实"}]

    with _session() as session:
        with pytest.raises(ValueError, match="too generic"):
            ensure_voice_profiles_for_accounts(session, tenant_id=1, account_ids=[101], generator=generator)


def test_ensure_voice_profiles_rejects_overly_similar_batch():
    def generator(account_ids: list[int]) -> list[dict]:
        assert account_ids == [101, 102]
        return [
            {
                "account_id": 101,
                "age_band": "青年",
                "sentence_length": "短句",
                "interaction_habits": ["爱追问价格"],
                "tone_strength": "轻松",
                "lexical_preferences": ["我看看"],
                "emoji_policy": "少用",
                "short_prompt_summary": "青年短句，爱追问价格，少表情，偶尔说我看看",
            },
            {
                "account_id": 102,
                "age_band": "青年",
                "sentence_length": "短句",
                "interaction_habits": ["爱追问价格"],
                "tone_strength": "轻松",
                "lexical_preferences": ["我看看"],
                "emoji_policy": "少用",
                "short_prompt_summary": "青年短句，爱追问价格，少表情，偶尔说我看看",
            },
        ]

    with _session() as session:
        with pytest.raises(ValueError, match="too similar"):
            ensure_voice_profiles_for_accounts(session, tenant_id=1, account_ids=[101, 102], generator=generator)


def test_group_stance_summaries_backfills_redis_cache(monkeypatch):
    fake_redis = FakeStanceRedis(values=[None])
    monkeypatch.setattr(account_stance_memory, "_redis_client", lambda _redis_url: fake_redis)
    monkeypatch.setattr(account_stance_memory, "get_settings", lambda: SimpleNamespace(queue_backend="redis", redis_url="redis://test"))

    with _session() as session:
        session.add(
            AiAccountGroupStanceMemory(
                tenant_id=1,
                group_id=7,
                account_id=101,
                summary="刚围绕花花老师表示观望，别突然强夸",
                topic_direction="郑州楼凤妹子怎么样",
                teacher_target="花花老师",
                stance="sent",
                last_act_type="观望",
                last_semantic_cluster="teacher_watch",
                last_message_id="tg-101",
            )
        )
        session.commit()

        result = group_stance_summaries(session, tenant_id=1, group_id=7, account_ids=[101])

    assert result == {101: "刚围绕花花老师表示观望，别突然强夸"}
    assert fake_redis.mget_calls == [["ai_group:stance:1:7:101"]]
    assert fake_redis.setex_calls
    refreshed = json.loads(fake_redis.setex_calls[0][2])
    assert refreshed["summary"] == "刚围绕花花老师表示观望，别突然强夸"
    assert refreshed["last_act_type"] == "light_disagree"


def test_upsert_group_stance_memory_refreshes_redis_cache(monkeypatch):
    fake_redis = FakeStanceRedis()
    monkeypatch.setattr(account_stance_memory, "_redis_client", lambda _redis_url: fake_redis)
    monkeypatch.setattr(account_stance_memory, "get_settings", lambda: SimpleNamespace(queue_backend="redis", redis_url="redis://test"))

    with _session() as session:
        upsert_group_stance_memory(
            session,
            tenant_id=1,
            group_id=7,
            account_id=101,
            topic_direction="精品榜",
            teacher_target="主任",
            stance="sent",
            act_type="追问",
            semantic_cluster="teacher_price_question",
            message_id="tg-stance-ok",
            summary="追问：主任这个可以先问价格",
        )

    assert fake_redis.setex_calls
    key, ttl, value = fake_redis.setex_calls[0]
    assert key == "ai_group:stance:1:7:101"
    assert ttl >= 86400
    refreshed = json.loads(value)
    assert refreshed["summary"] == "追问：主任这个可以先问价格"
    assert refreshed["last_act_type"] == "question"


def test_parse_voice_profile_pipe_lines_requires_complete_fields():
    raw = (
        "101|青年|做过夜场熟客|常点花花老师|短句|先问位置；爱追问照片|轻松|我看看；别跑空|少用|确实不错|"
        "青年短句先问位置和照片偶尔说别跑空\n"
        "102|中年|常帮朋友踩点|约过天津场子|中句|先讲经历；偶尔吐槽|谨慎|稳一点；别急|不用表情|感觉挺靠谱|"
        "中年中句先讲踩点经历说话谨慎不急"
    )

    profiles = _parse_voice_profile_payloads(raw, [101, 102])

    assert [profile["account_id"] for profile in profiles] == ["101", "102"]
    assert profiles[0]["interaction_habits"] == ["先问位置", "爱追问照片"]


def test_parse_voice_profile_pipe_lines_rejects_incomplete_line():
    with pytest.raises(RuntimeError, match="字段数量错误"):
        _parse_voice_profile_payloads("101|青年|字段太少", [101])


def test_parse_voice_profile_json_lines_accepts_compact_fields():
    raw = (
        '{"id":101,"age":"青年","px":["做过夜场熟客"],"cx":["常点花花老师"],"len":"短句",'
        '"habits":["先问位置","爱追问照片"],"tone":"轻松","words":["我看看","别跑空"],'
        '"emoji":"少用","ban":["确实不错"],"summary":"青年短句先问位置和照片偶尔说别跑空"}\n'
        '{"id":102,"age":"中年","px":["常帮朋友踩点"],"cx":["约过天津场子"],"len":"中句",'
        '"habits":["先讲经历","偶尔吐槽"],"tone":"谨慎","words":["稳一点","别急"],'
        '"emoji":"不用表情","ban":["感觉挺靠谱"],"summary":"中年中句先讲踩点经历说话谨慎不急"}'
    )

    profiles = _parse_voice_profile_payloads(raw, [101, 102])

    assert [profile["account_id"] for profile in profiles] == [101, 102]
    assert profiles[0]["lexical_preferences"] == ["我看看", "别跑空"]
    assert profiles[1]["emoji_policy"] == "不用表情"


def test_generate_voice_profiles_uses_compact_token_budget(monkeypatch):
    captured: dict[str, int] = {}

    def fake_post(credentials, prompt, temperature, max_tokens, **kwargs):  # noqa: ANN001
        captured["max_tokens"] = max_tokens
        captured["reasoning_retry_max_tokens"] = kwargs["reasoning_retry_max_tokens"]
        return (
            "101|青年|做过夜场熟客|常点花花老师|短句|先问位置；爱追问照片|轻松|我看看；别跑空|少用|确实不错|"
            "青年短句先问位置和照片偶尔说别跑空",
            SimpleNamespace(total_tokens=120),
        )

    monkeypatch.setattr("app.services.task_center.account_voice_profile_generation.ai_gateway._post_openai_compatible", fake_post)

    with _session() as session:
        _account(session, 101, "测试号")
        profiles = _generate_voice_profile_payloads(
            session,
            1,
            [101],
            SimpleNamespace(model_name="mimo-v2.5"),
            SimpleNamespace(temperature=0.7, max_tokens=8192),
        )

    assert captured == {
        "max_tokens": VOICE_PROFILE_INITIAL_MAX_TOKENS,
        "reasoning_retry_max_tokens": VOICE_PROFILE_RETRY_MAX_TOKENS,
    }
    assert profiles[0]["account_id"] == "101"


def test_generate_voice_profiles_refills_missing_accounts(monkeypatch):
    calls: list[str] = []

    def fake_post(credentials, prompt, temperature, max_tokens, **kwargs):  # noqa: ANN001
        calls.append(prompt)
        if "account_id=102" in prompt and "account_id=101" not in prompt:
            return (
                "102|中年|常帮朋友踩点|约过天津场子|中句|先讲经历；偶尔吐槽|谨慎|稳一点；别急|不用表情|感觉挺靠谱|"
                "中年中句先讲踩点经历说话谨慎不急",
                SimpleNamespace(total_tokens=90),
            )
        return (
            "101|青年|做过夜场熟客|常点花花老师|短句|先问位置；爱追问照片|轻松|我看看；别跑空|少用|确实不错|"
            "青年短句先问位置和照片偶尔说别跑空",
            SimpleNamespace(total_tokens=120),
        )

    monkeypatch.setattr("app.services.task_center.account_voice_profile_generation.ai_gateway._post_openai_compatible", fake_post)

    with _session() as session:
        _account(session, 101, "测试号1")
        _account(session, 102, "测试号2")
        profiles = _generate_voice_profile_payloads(
            session,
            1,
            [101, 102],
            SimpleNamespace(model_name="mimo-v2.5"),
            SimpleNamespace(temperature=0.7, max_tokens=8192),
        )

    assert [profile["account_id"] for profile in profiles] == ["101", "102"]
    assert len(calls) == 2
    assert "account_id=102" in calls[1]
    assert "account_id=101" not in calls[1]


def test_generate_voice_profiles_retries_malformed_batch_as_single_accounts(monkeypatch):
    calls: list[str] = []

    def fake_post(credentials, prompt, temperature, max_tokens, **kwargs):  # noqa: ANN001
        calls.append(prompt)
        if "account_id=101" in prompt and "account_id=102" in prompt:
            return ('{"id":101,"age":"青年" "px":["缺逗号"]}', SimpleNamespace(total_tokens=60))
        if "account_id=101" in prompt:
            return (
                '{"id":101,"age":"青年","px":["做过夜场熟客"],"cx":["常点花花老师"],"len":"短句",'
                '"habits":["先问位置"],"tone":"轻松","words":["我看看"],"emoji":"少用",'
                '"ban":["确实不错"],"summary":"青年短句先问位置偶尔说我看看"}',
                SimpleNamespace(total_tokens=90),
            )
        return (
            '{"id":102,"age":"中年","px":["常帮朋友踩点"],"cx":["约过天津场子"],"len":"中句",'
            '"habits":["先讲经历"],"tone":"谨慎","words":["稳一点"],"emoji":"不用表情",'
            '"ban":["感觉挺靠谱"],"summary":"中年中句先讲踩点经历说话谨慎"}',
            SimpleNamespace(total_tokens=90),
        )

    monkeypatch.setattr("app.services.task_center.account_voice_profile_generation.ai_gateway._post_openai_compatible", fake_post)

    with _session() as session:
        _account(session, 101, "测试号1")
        _account(session, 102, "测试号2")
        profiles = _generate_voice_profile_payloads(
            session,
            1,
            [101, 102],
            SimpleNamespace(model_name="mimo-v2.5"),
            SimpleNamespace(temperature=0.7, max_tokens=8192),
        )

    assert [profile["account_id"] for profile in profiles] == [101, 102]
    assert len(calls) == 3
    assert "account_id=101" in calls[1]
    assert "account_id=102" not in calls[1]


def test_ensure_voice_profiles_retries_overly_similar_batch_before_insert():
    calls = 0

    def generator(account_ids: list[int]) -> list[dict]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return [
                {"account_id": account_id, "short_prompt_summary": "青年短句，爱追问价格，少表情"}
                for account_id in account_ids
            ]
        return [
            {"account_id": 101, "short_prompt_summary": "青年短句，先问价格再看反馈"},
            {"account_id": 102, "short_prompt_summary": "中年中句，先看服务态度再接话"},
        ]

    with _session() as session:
        created = ensure_voice_profiles_for_accounts(session, tenant_id=1, account_ids=[101, 102], generator=generator)
        session.commit()

        rows = list(session.scalars(select(AiAccountVoiceProfile).order_by(AiAccountVoiceProfile.account_id)))
        assert calls == 2
        assert created == 2
        assert [row.short_prompt_summary for row in rows] == ["青年短句，先问价格再看反馈", "中年中句，先看服务态度再接话"]


def test_list_voice_profiles_searches_accounts_and_marks_missing_cards():
    with _session() as session:
        _account(session, 101, "花花号", "huahua")
        _account(session, 102, "新人号", "newgirl")
        session.add(_profile(101, "青年短句，爱追问价格，少表情"))
        session.commit()

        rows = list_voice_profiles(session, tenant_id=1, search="newgirl")

        assert len(rows) == 1
        assert rows[0]["account_id"] == 102
        assert rows[0]["profile_status"] == "missing"
        assert rows[0]["short_prompt_summary"] == ""


def test_list_voice_profiles_searches_status_and_updated_date():
    with _session() as session:
        _account(session, 101, "花花号", "huahua")
        _account(session, 102, "新人号", "newgirl")
        disabled = _profile(101, "青年短句，爱追问价格，少表情", status="disabled")
        session.add(disabled)
        session.add(_profile(102, "中年中句，谨慎补经历，偶尔轻吐槽"))
        session.commit()

        by_account_status = list_voice_profiles(session, tenant_id=1, search=AccountStatus.ACTIVE.value)
        by_profile_status = list_voice_profiles(session, tenant_id=1, search="disabled")
        updated_prefix = str(disabled.updated_at.date())
        by_updated_at = list_voice_profiles(session, tenant_id=1, search=updated_prefix)

        assert {row["account_id"] for row in by_account_status} == {101, 102}
        assert [row["account_id"] for row in by_profile_status] == [101]
        assert {row["account_id"] for row in by_updated_at} == {101, 102}


def test_patch_voice_profile_creates_next_version_and_audit_log():
    with _session() as session:
        _account(session, 101, "花花号")
        session.add(_profile(101, "青年短句，爱追问价格，少表情"))
        session.commit()

        row = patch_voice_profile(
            session,
            tenant_id=1,
            account_id=101,
            patch={"short_prompt_summary": "青年短句，先观望再追问，偶尔说我看看"},
            actor="tester",
        )
        session.commit()

        profiles = list(session.scalars(select(AiAccountVoiceProfile).order_by(AiAccountVoiceProfile.version)))
        audits = list(session.scalars(select(AuditLog)))
        assert row.version == 2
        assert row.updated_by == "tester"
        assert [profile.status for profile in profiles] == ["superseded", "active"]
        assert audits[0].action == "编辑账号表达卡"


def test_voice_profile_versions_include_all_versions_and_audit_rows():
    with _session() as session:
        _account(session, 101, "花花号")
        session.add(_profile(101, "青年短句，爱追问价格，少表情", version=1, status="superseded"))
        session.add(_profile(101, "青年短句，先观望再追问，偶尔说我看看", version=2, status="active"))
        session.add(AuditLog(tenant_id=1, actor="tester", action="编辑账号表达卡", target_type="ai_account_voice_profile", target_id="101", detail="version=2"))
        session.commit()

        versions = list_voice_profile_versions(session, tenant_id=1, account_id=101)
        audits = list_voice_profile_audits(session, tenant_id=1, account_id=101)

        assert [row["version"] for row in versions] == [2, 1]
        assert versions[0]["status"] == "active"
        assert versions[1]["short_prompt_summary"] == "青年短句，爱追问价格，少表情"
        assert audits[0]["action"] == "编辑账号表达卡"
        assert audits[0]["detail"] == "version=2"


def test_rollback_voice_profile_creates_new_active_version_and_audit_log():
    with _session() as session:
        _account(session, 101, "花花号")
        session.add(_profile(101, "青年短句，爱追问价格，少表情", version=1, status="superseded"))
        session.add(_profile(101, "青年短句，先观望再追问，偶尔说我看看", version=2, status="active"))
        session.commit()

        restored = rollback_voice_profile(session, tenant_id=1, account_id=101, source_version=1, actor="tester")
        session.commit()

        profiles = list(session.scalars(select(AiAccountVoiceProfile).order_by(AiAccountVoiceProfile.version)))
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "回滚账号表达卡"))
        assert restored.version == 3
        assert restored.status == "active"
        assert restored.short_prompt_summary == "青年短句，爱追问价格，少表情"
        assert [profile.status for profile in profiles] == ["superseded", "superseded", "active"]
        assert audit is not None
        assert audit.detail == "source_version=1,target_version=3"


def test_rebuild_voice_profile_keeps_existing_profile_when_generator_is_invalid():
    def generator(_account_ids: list[int]) -> list[dict]:
        return [{"account_id": 101, "short_prompt_summary": "自然、随意、真实"}]

    with _session() as session:
        _account(session, 101, "花花号")
        session.add(_profile(101, "青年短句，爱追问价格，少表情"))
        session.commit()

        with pytest.raises(ValueError, match="too generic"):
            rebuild_voice_profile(session, tenant_id=1, account_id=101, generator=generator, actor="tester")

        row = session.scalar(select(AiAccountVoiceProfile))
        assert row.version == 1
        assert row.status == "active"


def test_batch_rebuild_voice_profiles_generates_missing_accounts_only():
    def generator(account_ids: list[int]) -> list[dict]:
        assert account_ids == [102]
        return [{"account_id": 102, "short_prompt_summary": "中年中句，谨慎补经历，偶尔轻吐槽"}]

    with _session() as session:
        _account(session, 101, "花花号")
        _account(session, 102, "新人号")
        session.add(_profile(101, "青年短句，爱追问价格，少表情"))
        session.commit()

        result = batch_rebuild_voice_profiles(
            session,
            tenant_id=1,
            account_ids=[101, 102],
            generator=generator,
            actor="tester",
            missing_only=True,
        )
        session.commit()

        rows = list(session.scalars(select(AiAccountVoiceProfile).order_by(AiAccountVoiceProfile.account_id)))
        assert result["created"] == 1
        assert result["skipped"] == 1
        assert result["items"] == [
            {
                "account_id": 101,
                "status": "skipped",
                "version": 1,
                "similarity_score": None,
                "failure_reason": "",
                "skipped_reason": "已有生效表达卡",
            },
            {
                "account_id": 102,
                "status": "created",
                "version": 1,
                "similarity_score": 100,
                "failure_reason": "",
                "skipped_reason": "",
            },
        ]
        assert [row.account_id for row in rows] == [101, 102]


def test_batch_rebuild_missing_with_empty_account_ids_scans_all_active_accounts():
    def generator(account_ids: list[int]) -> list[dict]:
        assert account_ids == [102, 103]
        return [
            {"account_id": 102, "short_prompt_summary": "青年短句，先问价格再看反馈"},
            {"account_id": 103, "short_prompt_summary": "中年短句，先看服务态度再接话"},
        ]

    with _session() as session:
        _account(session, 101, "花花号")
        _account(session, 102, "新人号")
        _account(session, 103, "观察号")
        session.add(_profile(101, "青年短句，爱追问价格，少表情"))
        session.commit()

        result = batch_rebuild_voice_profiles(
            session,
            tenant_id=1,
            account_ids=[],
            generator=generator,
            actor="tester",
            missing_only=True,
        )
        session.commit()

        rows = list(session.scalars(select(AiAccountVoiceProfile).order_by(AiAccountVoiceProfile.account_id)))
        assert result["created"] == 2
        assert result["skipped"] == 1
        assert [item["status"] for item in result["items"]] == ["skipped", "created", "created"]
        assert result["items"][0]["skipped_reason"] == "已有生效表达卡"
        assert all(isinstance(item["similarity_score"], int) for item in result["items"][1:])
        assert [row.account_id for row in rows] == [101, 102, 103]


def test_batch_rebuild_voice_profiles_reports_quality_failures_without_insert():
    def generator(account_ids: list[int]) -> list[dict]:
        assert account_ids == [101, 102]
        return [
            {"account_id": account_id, "short_prompt_summary": "自然、随意、真实"}
            for account_id in account_ids
        ]

    with _session() as session:
        _account(session, 101, "花花号")
        _account(session, 102, "新人号")

        result = batch_rebuild_voice_profiles(
            session,
            tenant_id=1,
            account_ids=[101, 102],
            generator=generator,
            actor="tester",
            missing_only=False,
        )
        session.commit()

        assert result["created"] == 0
        assert result["skipped"] == 0
        assert [item["account_id"] for item in result["items"]] == [101, 102]
        assert [item["status"] for item in result["items"]] == ["failed", "failed"]
        assert all(item["version"] == 0 for item in result["items"])
        assert all(item["similarity_score"] is None for item in result["items"])
        assert all("too similar" in item["failure_reason"] for item in result["items"])
        assert all(item["skipped_reason"] == "" for item in result["items"])
        assert session.scalar(select(func.count(AiAccountVoiceProfile.id))) == 0


def test_batch_rebuild_voice_profiles_supersedes_existing_versions():
    def generator(account_ids: list[int]) -> list[dict]:
        assert account_ids == [101]
        return [{"account_id": 101, "short_prompt_summary": "中年中句，先看服务态度再接话"}]

    with _session() as session:
        _account(session, 101, "花花号")
        session.add(_profile(101, "青年短句，爱追问价格，少表情"))
        session.commit()

        result = batch_rebuild_voice_profiles(
            session,
            tenant_id=1,
            account_ids=[101],
            generator=generator,
            actor="tester",
            missing_only=False,
        )
        session.commit()

        rows = list(session.scalars(select(AiAccountVoiceProfile).order_by(AiAccountVoiceProfile.version)))
        assert result["created"] == 1
        assert result["skipped"] == 0
        assert result["items"][0]["version"] == 2
        assert [row.status for row in rows] == ["superseded", "active"]
        assert rows[1].short_prompt_summary == "中年中句，先看服务态度再接话"


def test_batch_update_voice_profile_status_disables_and_restores_profiles():
    with _session() as session:
        _account(session, 101, "花花号")
        _account(session, 102, "新人号")
        session.add(_profile(101, "青年短句，爱追问价格，少表情"))
        session.add(_profile(102, "中年中句，谨慎补经历，偶尔轻吐槽"))
        session.commit()

        disabled = batch_update_voice_profile_status(
            session,
            tenant_id=1,
            account_ids=[101, 102],
            status="disabled",
            actor="tester",
        )
        session.commit()

        assert disabled == {"updated": 2, "skipped": 0}
        assert voice_profile_prompt_summaries(session, tenant_id=1, account_ids=[101, 102]) == {}

        restored = batch_update_voice_profile_status(
            session,
            tenant_id=1,
            account_ids=[101],
            status="active",
            actor="tester",
        )
        session.commit()

        assert restored == {"updated": 1, "skipped": 0}
        assert voice_profile_prompt_summaries(session, tenant_id=1, account_ids=[101, 102]) == {
            101: "青年短句，爱追问价格，少表情",
        }
        audits = list(session.scalars(select(AuditLog).order_by(AuditLog.id)))
        assert [audit.action for audit in audits] == ["批量停用账号表达卡", "批量停用账号表达卡", "批量恢复账号表达卡"]


def test_group_stance_memory_upserts_and_reads_summary():
    with _session() as session:
        upsert_group_stance_memory(
            session,
            tenant_id=1,
            group_id=22,
            account_id=101,
            topic_direction="郑州楼凤妹子怎么样",
            teacher_target="花花老师",
            stance="观望但偏正向",
            act_type="context_reply",
            semantic_cluster="service_attitude",
            message_id="msg-1",
            summary="刚夸过花花老师服务态度，语气谨慎",
        )
        upsert_group_stance_memory(
            session,
            tenant_id=1,
            group_id=22,
            account_id=101,
            topic_direction="郑州楼凤妹子怎么样",
            teacher_target="花花老师",
            stance="观望",
            act_type="short_react",
            semantic_cluster="service_attitude",
            message_id="msg-2",
            summary="继续观望，别突然强夸",
        )
        session.commit()

        rows = list(session.scalars(select(AiAccountGroupStanceMemory)))
        assert len(rows) == 1
        assert rows[0].last_message_id == "msg-2"
        assert group_stance_summaries(session, tenant_id=1, group_id=22, account_ids=[101]) == {
            101: "继续观望，别突然强夸"
        }
