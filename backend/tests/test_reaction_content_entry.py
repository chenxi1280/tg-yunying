import hashlib

import pytest

from app.models import Action, ChannelMessageSourceRevision, OperationTarget
from app.services.task_center.channel_payloads import LikeMessagePayload
from app.services.task_center.dispatcher import _reaction_final_gate
from app.services.task_center.executors.channel_like_capability import message_reaction_plan, reaction_capability_revision
from engine_source_test_support import NOW, message, seed_source_session

pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("reaction_type", ["specific", "random"])
def test_real_planner_uses_full_revision_text_and_final_gate_rechecks(reaction_type):
    session, task, _, _ = seed_source_session()
    with session:
        target = session.get(OperationTarget, 1)
        target.available_reactions = ["🎉"]
        row = message(session, 100)
        # A positive preview must not override the complete immutable source.
        row.content_preview = "年度业务总结"
        text = "年度业务总结" + "项目说明" * 100 + "黑客攻击导致资金被盗，暂停运营"
        revision = ChannelMessageSourceRevision(tenant_id=1, channel_message_id=row.id,
            source_revision=1, source_remote_message_id=100, source_published_at=NOW,
            source_observed_at=NOW, source_text_snapshot=text,
            source_content_hash=hashlib.sha256(text.encode()).hexdigest(), observation_identity_hash="source")
        session.add(revision)
        session.flush()
        row.current_source_revision_id = revision.id
        plan = message_reaction_plan(session, task, row, config={"reaction_type": reaction_type, "reaction_scope": "configured"},
            reactions=["🎉"], quantity=1, seed_id="test")
        assert plan == []
        assert task.stats["reaction_capability_unavailable"]["reason_code"] == "reaction_intent_no_match"
        action = Action(id="reaction", tenant_id=1, task_id=task.id, task_type=task.type, action_type="like_message")
        payload = LikeMessagePayload(channel_id="-1001", channel_target_id=1, channel_message_id=row.id,
            message_id=100, source_revision_id=revision.id, reaction_emoji="🎉",
            reaction_source_content_hash=revision.source_content_hash,
            reaction_capability_revision=reaction_capability_revision(target))
        assert _reaction_final_gate(session, action, payload) == "reaction_intent_no_match"
        revision.source_text_snapshot = "庆祝活动顺利完成"
        assert message_reaction_plan(session, task, row, config={"reaction_type": reaction_type}, reactions=["🎉"], quantity=1, seed_id="test") == ["🎉"]
        assert _reaction_final_gate(session, action, payload) == ""


def test_unsupported_reaction_is_capability_block_not_content_block():
    session, task, _, _ = seed_source_session()
    with session:
        row = message(session, 100)
        assert message_reaction_plan(session, task, row, config={"reaction_type": "specific"}, reactions=["🎉"], quantity=1, seed_id="test") == []
        assert task.stats["reaction_capability_unavailable"]["reason_code"] == "reaction_capability_unavailable"
