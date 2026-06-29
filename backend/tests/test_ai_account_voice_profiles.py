from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountStatus, AiAccountGroupStanceMemory, AiAccountVoiceProfile, AuditLog, TgAccount
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

    monkeypatch.setattr("app.services.task_center.account_voice_profiles.ai_gateway._post_openai_compatible", fake_post)

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

    monkeypatch.setattr("app.services.task_center.account_voice_profiles.ai_gateway._post_openai_compatible", fake_post)

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
        assert result == {"created": 1, "skipped": 1}
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
        assert result == {"created": 2, "skipped": 1}
        assert [row.account_id for row in rows] == [101, 102, 103]


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
