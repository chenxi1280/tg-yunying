from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    OperationTarget,
    ReactionIntentPolicyRevision,
    SourceReactionIntentDecision,
    Task,
    Tenant,
    TgAccount,
)
from app.services.task_center.reaction_intent import (
    classify_emoji_intent,
    detect_negative_keywords,
    ensure_reaction_intent_policy,
    evaluate_source_reaction_intent,
    normalize_emoji,
    resolve_safe_reactions,
)

pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as sess:
        yield sess


def test_classify_emoji_intent():
    assert classify_emoji_intent("👍") == "positive"
    assert classify_emoji_intent("❤️") == "positive"
    assert classify_emoji_intent("🙏") == "support"
    assert classify_emoji_intent("🤝") == "support"
    assert classify_emoji_intent("🎉") == "celebrate"
    assert classify_emoji_intent("🚀") == "celebrate"
    assert classify_emoji_intent("🔥") == "celebrate"
    assert classify_emoji_intent("👀") == "neutral"


def test_detect_negative_keywords():
    assert detect_negative_keywords("项目遭遇黑客攻击，正在清算中") is True
    assert detect_negative_keywords("深切悼念离世同仁") is True
    assert detect_negative_keywords("维权群已建立，亏损严重") is True
    assert detect_negative_keywords("今天天气真好，社区新版本上线！") is False


def test_resolve_safe_reactions_normal_and_negative():
    configured = ["👍", "🎉", "❤️"]
    available = ["👍", "🎉", "❤️", "🔥"]

    # Normal positive post: celebrate allowed
    cands, decision, _ = resolve_safe_reactions(
        configured,
        available,
        content_text="欢迎新成员加入！",
    )
    assert decision == "confirmed"
    assert set(cands) == {"👍", "🎉", "❤️"}

    # Serious/negative post: celebrate (🎉) must be excluded!
    cands_neg, decision_neg, details_neg = resolve_safe_reactions(
        configured,
        available,
        content_text="系统发生严重故障维护，请用户注意安全！",
    )
    assert decision_neg == "confirmed"
    assert details_neg["has_negative_keywords"] is True
    assert "🎉" not in cands_neg
    assert set(cands_neg) == {"👍", "❤️"}


def test_resolve_safe_reactions_celebrate_only_under_negative():
    configured = ["🎉", "🚀"]
    available = ["👍", "🎉", "🚀"]
    cands, decision, _ = resolve_safe_reactions(
        configured,
        available,
        content_text="由于黑客盗取资金，项目暂停运营",
    )
    assert decision == "reaction_intent_no_match"
    assert cands == []


def test_evaluate_source_reaction_intent(session):
    tenant = Tenant(id=1, name="Test Tenant")
    account = TgAccount(
        id=1,
        tenant_id=1,
        display_name="account1",
        phone_masked="+12345678",
    )
    task = Task(
        id="task-1001",
        tenant_id=1,
        name="like-task",
        type="channel_like",
        status="running",
        type_config={"engagement_contract_version": "unified_engagement_v1"},
    )
    session.add_all([tenant, account, task])
    session.flush()

    record = evaluate_source_reaction_intent(
        session,
        task=task,
        account=account,
        source_revision="rev-001",
        content_text="黑客攻击致亏损，请大家维权",
        allowed_reactions=["👍", "🎉", "🔥"],
        configured_reactions=["🎉", "🔥", "👍"],
    )
    assert record.has_negative_keywords is True
    assert record.candidate_reactions == ["👍"]
    assert record.decision == "confirmed"
    assert record.chosen_reaction == "👍"
