from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountStatus, AiAccountGroupStanceMemory, AiAccountVoiceProfile, AuditLog, TgAccount
from app.services.task_center.account_voice_profiles import (
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

        rows = list(session.scalars(select(AiAccountVoiceProfile)))
        assert rows == []


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
