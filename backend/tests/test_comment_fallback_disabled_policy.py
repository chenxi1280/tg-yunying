from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import (
    ChannelCommentFallbackPoolSnapshot,
    CommentFallbackPolicySnapshot,
    ContentMixContract,
    ExecutionAttempt,
    FallbackShuffleBagCursor,
    Task,
)
from app.schemas.task_center import ChannelCommentConfig
from app.services.task_center import comment_fallback_selection as fallback
from channel_comment_dispatch_test_support import (
    comment_dispatch_session,
    seed_dispatch_scope,
)


pytestmark = pytest.mark.no_postgres


def _config(**changes) -> dict:
    return {
        "target_channel_id": 101,
        "engagement_contract_version": "unified_engagement_v1",
        "account_group_ids": [1],
        "account_selection_mode": "group",
        "channel_comment_grounding_v1_enabled": True,
        "ai_two_stage_enabled": True,
        "ai_model": "QA-primary",
        "ai_semantic_reviewer_model": "QA-review",
        "ai_content_route_v2_enabled": True,
        "ai_content_policy_version_id": "QA-policy",
        "ai_content_allowed_routes": ["general"],
        "daily_comment_cap": 12000,
        "planned_fallback_max_bps": 0,
        "unicode_emoji_enabled": False,
        "image_meme_enabled": False,
        "unicode_emoji_weight_bps": 0,
        "image_meme_weight_bps": 0,
        **changes,
    }


@pytest.mark.parametrize("validator", ["schema", "runtime"])
def test_explicitly_disabled_fallback_is_valid(validator) -> None:
    _validate(validator, _config())


@pytest.mark.parametrize("validator", ["schema", "runtime"])
@pytest.mark.parametrize("change", [
    {"planned_fallback_max_bps": 2000},
    {"unicode_emoji_weight_bps": 10000},
    {"image_meme_weight_bps": 10000},
])
def test_disabled_fallback_rejects_residual_budget_or_weights(validator, change):
    with pytest.raises(ValueError, match="comment_fallback_type_required"):
        _validate(validator, _config(**change))


@pytest.mark.parametrize("validator", ["schema", "runtime"])
def test_disabled_fallback_does_not_default_an_omitted_budget_to_zero(validator):
    config = _config()
    del config["planned_fallback_max_bps"]
    with pytest.raises(ValueError, match="comment_fallback_type_required"):
        _validate(validator, config)


def _validate(validator: str, config: dict) -> None:
    if validator == "schema":
        ChannelCommentConfig(**config)
        return
    fallback.validate_comment_fallback_config(None, 1, config)


def _freeze(session, task, *, contract_id="comment-contract-1", revision=1):
    return fallback.freeze_comment_fallback_contract(
        session, task, channel_message_id=41,
        comment_plan_revision=revision, content_mix_contract_id=contract_id,
    )


def test_disabled_new_preparation_has_no_fallback_artifacts(monkeypatch) -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = session.get(Task, action.task_id)
        task.type_config = _config()
        session.commit()
        monkeypatch.setattr(fallback, "ready_image_assets", lambda *_: pytest.fail(
            "零兜底准备不应读取图片素材",
        ))

        assert _freeze(session, task) is None
        session.flush()
        for model in (CommentFallbackPolicySnapshot,
                      ChannelCommentFallbackPoolSnapshot, FallbackShuffleBagCursor):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_existing_pool_and_unknown_keep_their_frozen_identity() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = session.get(Task, action.task_id)
        task.type_config = _config(
            unicode_emoji_enabled=True, unicode_emoji_weight_bps=10000,
        )
        pool = _freeze(session, task)
        attempt = ExecutionAttempt(
            tenant_id=1, action_id=action.id, account_id=101,
            status="result_unknown", result_snapshot={"remote_mutation_started": True},
        )
        session.add(attempt)
        session.commit()
        identity = (pool.id, pool.fallback_policy_snapshot_id, attempt.id)
        task.type_config = _config()
        task.config_revision = 2
        session.commit()

        assert _freeze(session, task).id == identity[0]
        session.refresh(attempt)
        assert attempt.status == "result_unknown"
        assert attempt.result_snapshot == {"remote_mutation_started": True}
        assert pool.fallback_policy_snapshot_id == identity[1]
        assert session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 1


def test_old_policy_can_materialize_its_pool_after_current_fallback_disabled():
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = session.get(Task, action.task_id)
        task.type_config = _config(
            unicode_emoji_enabled=True, unicode_emoji_weight_bps=10000,
        )
        old_pool = _freeze(session, task)
        policy_id = old_pool.fallback_policy_snapshot_id
        session.add(ContentMixContract(
            id="delayed-contract", tenant_id=1,
            content_mix_scope_key="delayed-comment", content_contract_version=1,
            scope_total_slots=1, allocation_seed="delayed-comment",
            reply_min_required_count=0, reply_planned_count=0, direct_planned_count=1,
        ))
        task.type_config = _config()
        task.config_revision = 2
        session.commit()

        restored_pool = _freeze(session, task, contract_id="delayed-contract")
        assert restored_pool.fallback_policy_snapshot_id == policy_id
        assert session.get(CommentFallbackPolicySnapshot, policy_id).unicode_enabled


def test_missing_old_policy_is_not_hidden_by_current_disabled_fallback():
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = session.get(Task, action.task_id)
        task.type_config = _config()
        task.config_revision = 2
        session.commit()

        with pytest.raises(RuntimeError, match="missing_for_plan_revision"):
            _freeze(session, task)


def test_zero_planned_ratio_keeps_legacy_emergency_policy():
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = session.get(Task, action.task_id)
        task.type_config = _config(
            engagement_contract_version="", unicode_emoji_enabled=True,
            unicode_emoji_weight_bps=10000,
        )
        pool = _freeze(session, task)
        policy = session.get(CommentFallbackPolicySnapshot, pool.fallback_policy_snapshot_id)
        assert policy.unicode_enabled
        assert policy.unicode_weight_bps == 10000
