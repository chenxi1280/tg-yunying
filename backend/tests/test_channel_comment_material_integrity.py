from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, CommentFallbackSelection, Material, MaterialGroup, Tenant
from app.schemas.ai_config import MaterialGroupCreate, MaterialGroupUpdate, MaterialUpdate
from app.services import ai_config as ai_config_service
from app.services.task_center import details
from app.services.task_center.comment_fallback_selection import (
    select_comment_fallback,
)
from channel_comment_dispatch_test_support import (
    comment_dispatch_session,
    seed_dispatch_scope,
)
from test_channel_comment_fallback_selection import (
    _enable_v2,
    _freeze,
    _seed_image_meme,
)


pytestmark = pytest.mark.no_postgres


def test_zip_import_creates_and_merges_explicit_material_group() -> None:
    with _material_session() as session:
        first = _zip_import(session, title="评论表情包", image_name="one.png")
        second = _zip_import(session, title="评论表情包", image_name="two.png")
        group = session.scalar(select(MaterialGroup))

        assert first.success_count == second.success_count == 1
        assert group.name == "评论表情包"
        assert group.group_type == "表情包"
        assert group.material_ids == [1, 2]
        assert group.membership_revision == 2


def test_zip_import_rejects_existing_group_with_different_type() -> None:
    with _material_session() as session:
        session.add(MaterialGroup(
            tenant_id=1, name="冲突包", group_type="图片", material_ids=[],
        ))
        session.commit()

        with pytest.raises(ValueError, match="material_import_group_type_conflict"):
            _zip_import(session, title="冲突包", image_name="one.png")

        assert list(session.scalars(select(Material))) == []


def test_material_type_change_is_blocked_while_material_is_group_member() -> None:
    with _material_session() as session:
        material = _material(session, material_type="表情包")
        session.add(MaterialGroup(
            tenant_id=1, name="评论包", group_type="表情包",
            material_ids=[material.id],
        ))
        session.commit()

        with pytest.raises(ValueError, match="material_group_member_type_change_blocked"):
            ai_config_service.update_material(
                session, material.id, MaterialUpdate(material_type="图片"), "qa",
            )

        assert session.get(Material, material.id).material_type == "表情包"


def test_invalid_group_does_not_break_material_group_list() -> None:
    with _material_session() as session:
        material = _material(session, material_type="图片")
        session.add_all([
            MaterialGroup(
                tenant_id=1, name="坏组", group_type="表情包",
                material_ids=[material.id],
            ),
            MaterialGroup(
                tenant_id=1, name="正常组", group_type="表情包", material_ids=[],
            ),
        ])
        session.commit()

        groups = ai_config_service.list_material_groups(session, 1)

        assert [item.membership_state for item in groups] == ["invalid", "ready"]
        assert groups[0].ready_image_meme_count == 0


def test_reference_summary_counts_group_pool_and_selection() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        _seed_image_meme(session)
        task = _enable_v2(session, action, unicode_weight=0, image_weight=10000)
        _freeze(session, task)
        select_comment_fallback(
            session, action_id=action.id, tenant_id=1, task_id=task.id,
            content_mix_contract_id="comment-contract-1", target_ordinal=1,
            fallback_reason="qa",
        )

        summary = ai_config_service.material_reference_summary(session, 1, 201)

        assert summary.material_group_count == 1
        assert summary.fallback_pool_count == 1
        assert summary.fallback_selection_count == 1
        assert summary.total_count >= 3


def test_material_group_update_uses_membership_revision_cas() -> None:
    with _material_session() as session:
        group = MaterialGroup(
            tenant_id=1, name="并发组", group_type="表情包", material_ids=[],
        )
        session.add(group)
        session.commit()

        updated = ai_config_service.update_material_group(
            session, 1, group.id,
            MaterialGroupUpdate(expected_membership_revision=1, is_active=False),
            "qa",
        )
        assert updated.membership_revision == 2
        with pytest.raises(ValueError, match="material_group_revision_conflict:2"):
            ai_config_service.update_material_group(
                session, 1, group.id,
                MaterialGroupUpdate(
                    expected_membership_revision=1, description="stale",
                ),
                "qa",
            )


def test_metadata_update_does_not_clear_membership_review_state() -> None:
    with _material_session() as session:
        group = MaterialGroup(
            tenant_id=1, name="待处理组", group_type="表情包", material_ids=[],
            membership_state="review_required",
            membership_state_reason="ambiguous_same_type_groups",
        )
        session.add(group)
        session.commit()

        updated = ai_config_service.update_material_group(
            session, 1, group.id,
            MaterialGroupUpdate(
                expected_membership_revision=1, description="仅补充说明",
            ),
            "qa",
        )

        assert updated.membership_state == "review_required"
        assert updated.membership_state_reason == "ambiguous_same_type_groups"


def test_group_with_members_requires_explicit_type() -> None:
    with _material_session() as session:
        material = _material(session, material_type="表情包")

        with pytest.raises(ValueError, match="material_group_type_required"):
            ai_config_service.create_material_group(
                session, 1,
                MaterialGroupCreate(name="无类型组", material_ids=[material.id]),
                "qa",
            )


def test_material_group_name_cannot_be_whitespace() -> None:
    with _material_session() as session:
        with pytest.raises(ValueError, match="material_group_name_required"):
            ai_config_service.create_material_group(
                session, 1, MaterialGroupCreate(name="   "), "qa",
            )


def test_planned_fallback_kind_is_persisted_from_explicit_intent() -> None:
    with comment_dispatch_session() as session:
        action = seed_dispatch_scope(session)
        task = _enable_v2(session, action, unicode_weight=10000, image_weight=0)
        _freeze(session, task)

        selected = select_comment_fallback(
            session, action_id=action.id, tenant_id=1, task_id=task.id,
            content_mix_contract_id="comment-contract-1", target_ordinal=1,
            fallback_reason="planned_capacity", fallback_kind="planned",
        )

        assert selected.metadata["fallback_kind"] == "planned"


def test_detail_counts_selected_separately_from_remote_confirmed() -> None:
    payload = {
        "content_source": "comment_unicode_emoji_fallback",
        "comment_fallback_selection": {"fallback_kind": "planned"},
    }
    item = {"stats": {}}
    failed = Action(id="failed", action_type="post_comment", status="failed", result={})
    details._apply_comment_fallback_group(item, failed, payload, pools={})
    assert item["stats"]["unicode_emoji_fallback_selected"] == 1
    assert item["stats"].get("unicode_emoji_fallback_remote_confirmed", 0) == 0

    fact = {
        "fact_type": "channel_comment", "action_id": "sent",
        "content_source": "comment_unicode_emoji_fallback",
        "remote_message_id": "9001",
    }
    sent = Action(
        id="sent", action_type="post_comment", status="success",
        result={"channel_comment_remote_fact": fact},
    )
    details._apply_comment_fallback_group(item, sent, payload, pools={})
    assert item["stats"]["unicode_emoji_fallback_selected"] == 2
    assert item["stats"]["unicode_emoji_fallback_remote_confirmed"] == 1
    assert item["stats"]["planned_fallback_remote_confirmed"] == 1


def test_detail_uses_persistent_selection_when_action_payload_write_is_missing() -> None:
    selection = CommentFallbackSelection(
        id="selection-read-model",
        tenant_id=1,
        task_id="task-1",
        content_mix_contract_id="contract-1",
        target_ordinal=1,
        assignment_version=1,
        selection_attempt=2,
        fallback_kind="planned",
        fallback_content_kind="unicode_emoji",
        selection_seed="seed",
        selection_cycle=0,
        selection_rank=1,
        unicode_emoji="🔥",
        fallback_reason="planned_capacity",
        selection_state="ready",
    )
    item = {"stats": {}}
    action = Action(id="pending", action_type="post_comment", status="executing")

    details._apply_comment_fallback_group(
        item, action, {}, pools={}, selection=selection,
    )

    assert item["stats"]["unicode_emoji_fallback_selected"] == 1
    assert item["stats"]["planned_fallback_selected"] == 1
    assert item["fallback_selections"] == [{
        "selection_id": "selection-read-model",
        "selection_state": "ready",
        "fallback_kind": "planned",
        "fallback_content_kind": "unicode_emoji",
        "unicode_emoji": "🔥",
        "material_id": None,
        "asset_version_id": None,
        "selection_attempt": 2,
        "fallback_reason": "planned_capacity",
    }]


def _material_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="material-integrity"))
    session.commit()
    return session


def _zip_import(session: Session, *, title: str, image_name: str):
    archive = BytesIO()
    with ZipFile(archive, "w") as file:
        file.writestr(image_name, b"\x89PNG\r\n\x1a\nimage" + image_name.encode())
    return ai_config_service.create_material_zip_import(
        session, tenant_id=1, title=title, material_type="表情包",
        tags="", caption="", filename="memes.zip",
        data=archive.getvalue(), actor="qa",
    )


def _material(session: Session, *, material_type: str) -> Material:
    material = Material(
        tenant_id=1, title="素材", material_type=material_type,
        content="https://example.com/material.png", source_kind="url",
    )
    session.add(material)
    session.flush()
    return material
