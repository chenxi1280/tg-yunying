from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from app.integrations.telegram import SendResult
from app.models import (
    CommentFulfillmentObligation,
    CommentFallbackSelection,
    ExecutionAttempt,
    FallbackShuffleBagCursor,
    Material,
    MaterialAssetVersion,
    MaterialGroup,
    MaterialTgRefVersion,
    Task,
)
from app.services.task_center import dispatcher
from app.services._common import _now
from app.services.task_center.comment_fallback_selection import (
    UNICODE_EMOJI_ALLOWLIST_V2,
    freeze_comment_fallback_contract,
    select_comment_fallback,
    validate_comment_fallback_config,
)
from app.services.task_center.comment_fallback_materials import ready_image_assets
from app.services.task_center.comment_fallback_materials import static_image
from app.services.task_center.comment_generation_dispatch import (
    CommentGenerationDependencies,
)
from app.schemas.task_center import ChannelCommentConfig, TaskSettingsUpdate
from channel_comment_dispatch_test_support import (
    comment_dispatch_session,
    dispatch_generated_comment_action,
    seed_dispatch_scope,
)


pytestmark = pytest.mark.no_postgres


def test_unicode_shuffle_bag_is_persisted_and_non_repeating() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = _enable_v2(session, action, unicode_weight=10000, image_weight=0)
        _freeze(session, task)

        selected = [
            select_comment_fallback(
                session,
                action_id=action.id,
                tenant_id=1,
                task_id=task.id,
                content_mix_contract_id="comment-contract-1",
                target_ordinal=ordinal,
                fallback_reason="quality_exhausted",
            )
            for ordinal in range(1, 21)
        ]

        assert len({item.content for item in selected}) == 20
        assert {item.content for item in selected} == set(UNICODE_EMOJI_ALLOWLIST_V2)
        replay = select_comment_fallback(
            session,
            action_id=action.id,
            tenant_id=1,
            task_id=task.id,
            content_mix_contract_id="comment-contract-1",
            target_ordinal=1,
            fallback_reason="retry",
        )
        assert replay.metadata["selection_id"] == selected[0].metadata["selection_id"]
        cursor = session.scalar(select(FallbackShuffleBagCursor))
        assert cursor.cycle == 1
        assert cursor.next_rank == 0


def test_image_pool_freezes_versions_and_crosses_to_unicode_when_empty() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        _seed_image_meme(session)
        task = _enable_v2(session, action, unicode_weight=0, image_weight=10000)
        validate_comment_fallback_config(session, 1, task.type_config)
        session.get(Material, 201).cache_ready_status = "not_cached"
        _freeze(session, task)

        selected = select_comment_fallback(
            session,
            action_id=action.id,
            tenant_id=1,
            task_id=task.id,
            content_mix_contract_id="comment-contract-1",
            target_ordinal=1,
            fallback_reason="quality_exhausted",
        )

        assert selected.content_kind == "unicode_emoji"
        assert selected.content in UNICODE_EMOJI_ALLOWLIST_V2
        row = session.get(CommentFallbackSelection, selected.metadata["selection_id"])
        assert row.fallback_reason == "image_meme_unavailable_unicode_fallback"


def test_image_shuffle_bag_uses_every_frozen_asset_before_repeat() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        _seed_image_meme(session, material_id=201)
        _seed_image_meme(session, material_id=202)
        task = _enable_v2(
            session, action, unicode_weight=0, image_weight=10000,
            unicode_enabled=False,
        )
        _freeze(session, task)

        selected = [
            select_comment_fallback(
                session,
                action_id=action.id,
                tenant_id=1,
                task_id=task.id,
                content_mix_contract_id="comment-contract-1",
                target_ordinal=ordinal,
                fallback_reason="planned_fallback",
            )
            for ordinal in (1, 2, 3)
        ]

        material_ids = [item.metadata["material_id"] for item in selected]
        assert set(material_ids[:2]) == {201, 202}
        assert material_ids[2] == material_ids[0]


def test_direct_image_fallback_reaches_channel_media_gateway(monkeypatch) -> None:
    observed: dict = {}
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        _seed_image_meme(session)
        task = _enable_v2(
            session, action, unicode_weight=0, image_weight=10000,
            unicode_enabled=False,
        )
        _freeze(session, task)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_: object())
        _install_image_gateway(monkeypatch, observed)

        assert dispatch_generated_comment_action(
            session,
            action,
            comment_generation_dependencies=CommentGenerationDependencies(
                direct_generator=_provider_down,
                reply_generator=_provider_down,
            ),
        ) is True
        assert action.status == "success"
        assert action.payload["comment_text"] == ""
        assert action.payload["comment_fallback_kind"] == "image_meme"
        assert action.payload["content_source"] == "comment_image_meme_fallback"
        assert observed["segment"]["emoji_asset_kind"] == "image_meme"
        assert observed["segment"]["caption"] == ""
        assert observed["reply_to"] is None
        obligation = session.get(CommentFulfillmentObligation, "comment-obligation-1")
        attempt = session.scalar(select(ExecutionAttempt))
        assert obligation.status == "confirmed"
        fact = attempt.result_snapshot["channel_comment_remote_fact"]
        assert fact == _expected_image_fact(action, attempt, reply=False)


def test_frozen_grounding_reply_never_reaches_fallback_after_flag_disabled(monkeypatch) -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session, reply=True)
        _seed_image_meme(session)
        task = _enable_v2(
            session, action, unicode_weight=0, image_weight=10000,
            unicode_enabled=False,
        )
        _freeze(session, task)
        action.payload = {
            **action.payload,
            "grounding_assignment_id": "frozen-assignment-1",
        }
        task.type_config = {
            **task.type_config,
            "channel_comment_grounding_v1_enabled": False,
        }
        session.commit()
        monkeypatch.setattr(
            dispatcher.gateway,
            "reply_channel_message",
            lambda *_args, **_kwargs: pytest.fail("回复质量失败不能调用文本 Gateway"),
        )
        monkeypatch.setattr(
            dispatcher.gateway,
            "reply_channel_media",
            lambda *_args, **_kwargs: pytest.fail("回复质量失败不能调用媒体 Gateway"),
        )

        assert dispatch_generated_comment_action(
            session,
            action,
            comment_generation_dependencies=CommentGenerationDependencies(
                direct_generator=_provider_down,
                reply_generator=_provider_down,
            ),
        ) is True
        session.refresh(action)
        assert action.status == "pending"
        assert action.payload["ai_generation_status"] == "pending"
        assert action.payload["comment_lifecycle_state"] == "reply_quality_shortfall"
        assert session.scalar(select(CommentFallbackSelection)) is None


def _install_image_gateway(monkeypatch, observed: dict) -> None:
    monkeypatch.setattr(
        dispatcher.gateway,
        "reply_channel_message",
        lambda *_args, **_kwargs: pytest.fail("图片兜底不能走文本 Gateway"),
    )

    def send_media(_account, _peer, *, segment, **kwargs):
        reply_to = kwargs.get("reply_to_message_id")
        observed.update({"segment": segment, "reply_to": reply_to})
        return SendResult(
            True,
            remote_message_id="image-comment-1",
            remote_mutation_started=True,
            remote_fact={
                "fact_type": "channel_comment",
                "content_kind": "image_meme",
                "remote_media_kind": "image_meme",
                "relation_kind": "reply" if reply_to else "direct",
                "reply_to_message_id": reply_to,
            },
        )

    monkeypatch.setattr(dispatcher.gateway, "reply_channel_media", send_media)


def _provider_down(*_args, **_kwargs):
    raise RuntimeError("provider down")


def _expected_image_fact(action, attempt, *, reply: bool) -> dict:
    selection = action.payload["comment_fallback_selection"]
    return {
        "fact_type": "channel_comment",
        "content_kind": "image_meme",
        "remote_media_kind": "image_meme",
        "relation_kind": "reply" if reply else "direct",
        "reply_to_message_id": 8101 if reply else None,
        "remote_message_id": "image-comment-1",
        "action_id": action.id,
        "execution_attempt_id": attempt.id,
        "content_source": "comment_image_meme_fallback",
        "fallback_kind": "emergency",
        "fallback_reason": action.payload["fallback_reason"],
        "selection_id": selection["selection_id"],
        "unicode_emoji": None,
        "unicode_emoji_hash": None,
        "outbound_content_hash": None,
        "accepted_content_hash": None,
        "fallback_content_hash": None,
        "quality_contract_version": "channel_comment_grounding_quality_v1",
        "outbound_media_fingerprint": dispatcher._comment_media_fingerprint(
            action.payload
        ),
        "material_id": 201,
        "asset_version_id": 1,
        "asset_fingerprint": "asset-201-v1",
        "tg_ref_version_id": 1,
        "asset_pool_hash": selection["asset_pool_hash"],
        "selection_attempt": 1,
    }


def test_material_shortfall_keeps_unavailable_selection_audit() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        _seed_image_meme(session)
        task = _enable_v2(
            session, action, unicode_weight=0, image_weight=10000,
            unicode_enabled=False,
        )
        _freeze(session, task)
        session.get(Material, 201).cache_ready_status = "not_cached"
        session.commit()

        with pytest.raises(RuntimeError, match="fallback_material_shortfall"):
            select_comment_fallback(
                session,
                action_id=action.id,
                tenant_id=1,
                task_id=task.id,
                content_mix_contract_id="comment-contract-1",
                target_ordinal=1,
                fallback_reason="quality_exhausted",
            )

        row = session.scalar(select(CommentFallbackSelection))
        assert row.selection_state == "material_unavailable"
        assert row.material_id == 201


def test_gateway_started_image_selection_is_never_reselected() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        _seed_image_meme(session)
        task = _enable_v2(
            session, action, unicode_weight=0, image_weight=10000,
            unicode_enabled=False,
        )
        _freeze(session, task)
        selected = select_comment_fallback(
            session,
            action_id=action.id,
            tenant_id=1,
            task_id=task.id,
            content_mix_contract_id="comment-contract-1",
            target_ordinal=1,
            fallback_reason="planned_fallback",
        )
        session.add(ExecutionAttempt(
            tenant_id=1,
            action_id=action.id,
            account_id=101,
            status="executing",
            gateway_call_started_at=_now(),
        ))
        session.get(Material, 201).cache_ready_status = "not_cached"
        session.commit()

        with pytest.raises(RuntimeError, match="fallback_selection_locked_after_gateway"):
            select_comment_fallback(
                session,
                action_id=action.id,
                tenant_id=1,
                task_id=task.id,
                content_mix_contract_id="comment-contract-1",
                target_ordinal=1,
                fallback_reason="retry",
            )

        rows = list(session.scalars(select(CommentFallbackSelection)))
        assert len(rows) == 1
        assert rows[0].id == selected.metadata["selection_id"]


def test_unicode_fallback_requires_matching_remote_fact(monkeypatch) -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = _enable_v2(session, action, unicode_weight=10000, image_weight=0)
        _freeze(session, task)
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_: object())

        def send_text(_account, _peer, **_kwargs):
            return SendResult(
                True,
                remote_message_id="unicode-comment-1",
                remote_mutation_started=True,
                remote_fact={
                    "fact_type": "channel_comment",
                    "content_kind": "text",
                    "relation_kind": "direct",
                    "reply_to_message_id": None,
                },
            )

        monkeypatch.setattr(dispatcher.gateway, "reply_channel_message", send_text)
        def failing(*_args, **_kwargs):
            raise RuntimeError("provider down")

        assert dispatch_generated_comment_action(
            session,
            action,
            comment_generation_dependencies=CommentGenerationDependencies(
                direct_generator=failing,
                reply_generator=failing,
            ),
        ) is True

        obligation = session.get(CommentFulfillmentObligation, "comment-obligation-1")
        fact = session.scalar(select(ExecutionAttempt)).result_snapshot[
            "channel_comment_remote_fact"
        ]
        assert obligation.status == "confirmed"
        assert fact["unicode_emoji"] == action.payload["comment_text"]
        assert fact["unicode_emoji_hash"] == hashlib.sha256(
            action.payload["comment_text"].encode()
        ).hexdigest()
        assert fact["outbound_content_hash"] == fact["unicode_emoji_hash"]
        assert fact["action_id"] == action.id
        assert fact["execution_attempt_id"]
        assert action.payload["comment_text"] in UNICODE_EMOJI_ALLOWLIST_V2


def test_frozen_contract_still_requires_fact_after_task_flag_disabled(
    monkeypatch,
) -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = _enable_v2(session, action, unicode_weight=10000, image_weight=0)
        _freeze(session, task)
        task.type_config = {
            **task.type_config,
            "channel_comment_grounding_v1_enabled": False,
        }
        session.commit()
        monkeypatch.setattr(dispatcher, "credentials_for_account", lambda *_: object())
        monkeypatch.setattr(
            dispatcher.gateway,
            "reply_channel_message",
            lambda *_args, **_kwargs: SendResult(
                True,
                remote_message_id="comment-without-fact",
                remote_mutation_started=True,
            ),
        )
        def failing(*_args, **_kwargs):
            raise RuntimeError("provider down")

        assert dispatch_generated_comment_action(
            session,
            action,
            comment_generation_dependencies=CommentGenerationDependencies(
                direct_generator=failing,
                reply_generator=failing,
            ),
        ) is True

        obligation = session.get(CommentFulfillmentObligation, "comment-obligation-1")
        assert action.status == "success"
        assert obligation.status != "confirmed"


def test_missing_historical_policy_snapshot_fails_closed() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        _seed_image_meme(session)
        task = _enable_v2(session, action, unicode_weight=10000, image_weight=0)
        task.config_revision = 2
        session.commit()

        with pytest.raises(
            RuntimeError,
            match="fallback_policy_snapshot_missing_for_plan_revision",
        ):
            freeze_comment_fallback_contract(
                session,
                task,
                channel_message_id=41,
                comment_plan_revision=1,
                content_mix_contract_id="comment-contract-1",
            )


def test_schema_rejects_incomplete_grounding_activation() -> None:
    with pytest.raises(ValueError, match="channel_comment_grounding_activation_incomplete"):
        ChannelCommentConfig(
            target_channel_id=1,
            channel_comment_grounding_v1_enabled=True,
        )


def test_zero_weight_image_type_does_not_require_material_group() -> None:
    config = ChannelCommentConfig(
        target_channel_id=1,
        ai_model="generator",
        ai_two_stage_enabled=True,
        ai_semantic_reviewer_model="reviewer",
        ai_content_route_v2_enabled=True,
        ai_content_policy_version_id="policy-1",
        ai_content_allowed_routes=["general"],
        channel_comment_grounding_v1_enabled=True,
        unicode_emoji_enabled=True,
        image_meme_enabled=True,
        image_meme_material_group_id=None,
        daily_comment_cap=10,
        unicode_emoji_weight_bps=10000,
        image_meme_weight_bps=0,
    )

    assert config.image_meme_enabled is True
    assert config.image_meme_material_group_id is None


def test_image_meme_groups_use_explicit_membership() -> None:
    with comment_dispatch_session() as session:
        _seed_image_meme(session, material_id=201)
        _seed_image_meme(session, material_id=202)
        session.get(MaterialGroup, 81).material_ids = [201]
        session.add(MaterialGroup(
            id=82,
            tenant_id=1,
            name="另一个图片表情包",
            group_type="表情包",
            material_ids=[202],
        ))
        session.commit()

        assert [item[0].id for item in ready_image_assets(session, 1, 81)] == [201]
        assert [item[0].id for item in ready_image_assets(session, 1, 82)] == [202]


def test_image_meme_pool_rejects_non_static_image_files() -> None:
    assert static_image(Material(mime_type="image/webp", file_name="ok.webp"))
    assert not static_image(Material(mime_type="image/gif", file_name="bad.gif"))
    assert not static_image(Material(mime_type="image/svg+xml", file_name="bad.svg"))
    assert not static_image(Material(mime_type="application/pdf", file_name="bad.pdf"))


def test_disabled_selected_image_is_not_reused() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        _seed_image_meme(session)
        task = _enable_v2(
            session, action, unicode_weight=0, image_weight=10000,
            unicode_enabled=False,
        )
        _freeze(session, task)
        selected = select_comment_fallback(
            session,
            action_id=action.id,
            tenant_id=1,
            task_id=task.id,
            content_mix_contract_id="comment-contract-1",
            target_ordinal=1,
            fallback_reason="provider_failed",
        )
        session.get(Material, 201).review_status = "已禁用"
        session.commit()

        with pytest.raises(RuntimeError, match="fallback_material_shortfall"):
            select_comment_fallback(
                session,
                action_id=action.id,
                tenant_id=1,
                task_id=task.id,
                content_mix_contract_id="comment-contract-1",
                target_ordinal=1,
                fallback_reason="retry",
            )

        rows = list(session.scalars(select(CommentFallbackSelection)))
        assert rows[0].id == selected.metadata["selection_id"]
        assert all(row.selection_state == "material_unavailable" for row in rows)


def test_settings_patch_accepts_fallback_policy_fields() -> None:
    patch = TaskSettingsUpdate(
        channel_comment_grounding_v1_enabled=True,
        unicode_emoji_enabled=True,
        image_meme_enabled=True,
        image_meme_material_group_id=81,
        unicode_emoji_weight_bps=6000,
        image_meme_weight_bps=4000,
        allow_image_reselection_before_gateway=True,
        allow_cross_kind_fallback_to_unicode=True,
    )

    assert patch.model_dump(exclude_unset=True) == {
        "channel_comment_grounding_v1_enabled": True,
        "unicode_emoji_enabled": True,
        "image_meme_enabled": True,
        "image_meme_material_group_id": 81,
        "unicode_emoji_weight_bps": 6000,
        "image_meme_weight_bps": 4000,
        "allow_image_reselection_before_gateway": True,
        "allow_cross_kind_fallback_to_unicode": True,
    }


def test_runtime_config_rejects_image_group_without_ready_assets() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = _enable_v2(
            session, action, unicode_weight=0, image_weight=10000,
            unicode_enabled=False,
        )

        with pytest.raises(ValueError, match="image_meme_material_group_has_no_ready_assets"):
            validate_comment_fallback_config(session, 1, task.type_config)


def _enable_v2(
    session,
    action,
    *,
    unicode_weight: int,
    image_weight: int,
    unicode_enabled: bool = True,
) -> Task:
    task = session.get(Task, action.task_id)
    task.type_config = {
        **task.type_config,
        "channel_comment_grounding_v1_enabled": True,
        "unicode_emoji_enabled": unicode_enabled,
        "image_meme_enabled": image_weight > 0,
        "image_meme_material_group_id": 81 if image_weight > 0 else None,
        "unicode_emoji_weight_bps": unicode_weight,
        "image_meme_weight_bps": image_weight,
        "allow_image_reselection_before_gateway": True,
        "allow_cross_kind_fallback_to_unicode": True,
    }
    session.commit()
    return task


def _freeze(session, task: Task) -> None:
    freeze_comment_fallback_contract(
        session,
        task,
        channel_message_id=41,
        comment_plan_revision=1,
        content_mix_contract_id="comment-contract-1",
    )
    session.commit()


def _seed_image_meme(session, *, material_id: int = 201) -> None:
    if session.get(MaterialGroup, 81) is None:
        session.add(MaterialGroup(
            id=81, tenant_id=1, name="评论图片表情", group_type="表情包",
            material_ids=[material_id],
        ))
    else:
        group = session.get(MaterialGroup, 81)
        group.material_ids = sorted({*(group.material_ids or []), material_id})
    cache_message_id = str(material_id)
    session.add(Material(
        id=material_id, tenant_id=1, title=f"围观 {material_id}", material_type="表情包",
        content="https://example.invalid/meme.webp", review_status="已审核",
        asset_fingerprint=f"asset-{material_id}-v1", asset_version_id=1,
        delivery_mode="download_reupload", emoji_asset_kind="image_meme",
        cache_ready_status="ready", tg_cache_peer_id="-100900",
        tg_cache_message_id=cache_message_id, tg_ref_version_id=1,
        mime_type="image/webp", file_name="watch.webp",
    ))
    session.add(MaterialAssetVersion(
        tenant_id=1, material_id=material_id, asset_version_id=1,
        asset_fingerprint=f"asset-{material_id}-v1", mime_type="image/webp",
        file_name="watch.webp",
    ))
    session.add(MaterialTgRefVersion(
        tenant_id=1, material_id=material_id, asset_version_id=1,
        tg_ref_version_id=1, cache_status="ready",
        tg_cache_peer_id="-100900", tg_cache_message_id=cache_message_id,
        gateway_type="telethon",
    ))
    session.flush()
