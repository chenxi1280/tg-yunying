from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import ContentKeywordRule, Task, Tenant
from app.schemas.operations_center import RuleSetCreate, RuleSetVersionCreate
from app.services.operations_center import (
    copy_rule_set_version,
    create_rule_set,
    create_rule_set_version,
    list_rule_set_bound_tasks,
    publish_rule_set_version,
    rollback_rule_set_version,
    rule_center_summary,
    test_rules as preview_rules,
)
from app.services.rule_engine import apply_output_policy, bound_rule_version, transform_content
from app.services.task_center.executors.group_relay import effective_relay_config


def test_rule_set_create_persists_task_scope_and_output_checks():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        created = create_rule_set(
            session,
            1,
            RuleSetCreate(
                name="AI 回复安全规则",
                task_types=["group_ai_chat"],
                default_policy={"output_failure": "transform_once_drop"},
                output_checks={"forbidden_keywords": ["引流"], "failure_strategy": "transform_once_drop"},
                transforms={"keyword_replacements": {"引流": "活动"}},
            ),
            "tester",
        )

    assert created.task_types == ["group_ai_chat"]
    assert created.default_policy["output_failure"] == "transform_once_drop"
    assert created.versions[0].output_checks["forbidden_keywords"] == ["引流"]


def test_rule_tester_validates_ai_candidates_one_by_one():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        rule_set = create_rule_set(
            session,
            1,
            RuleSetCreate(
                name="AI 候选校验",
                task_types=["group_ai_chat"],
                output_checks={"forbidden_keywords": ["风险"], "failure_strategy": "transform_once_drop"},
                transforms={"keyword_replacements": {"风险": "正常"}},
            ),
            "tester",
        )
        result = preview_rules(
            session,
            1,
            "用户消息",
            test_type="group_ai_chat",
            candidates=["第一条正常", "第二条风险内容"],
            rule_set_version_id=rule_set.active_version_id,
        )

    assert [item.passed for item in result.output_candidates] == [True, True]
    assert result.output_candidates[1].action == "transform"
    assert result.output_candidates[1].transformed_text == "第二条正常内容"


def test_published_rule_version_is_immutable_by_new_draft_flow():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        rule_set = create_rule_set(session, 1, RuleSetCreate(name="转发规则", task_types=["group_relay"]), "tester")
        draft = create_rule_set_version(
            session,
            1,
            rule_set.id,
            RuleSetVersionCreate(version_note="收紧输出校验", output_checks={"forbid_links": True}),
            "tester",
        )
        draft_version = next(version for version in draft.versions if version.status == "draft")
        published = publish_rule_set_version(session, 1, rule_set.id, draft_version.id, "tester")

    statuses = {version.version: version.status for version in published.versions}
    assert statuses == {2: "published", 1: "archived"}
    assert published.active_version_id == draft_version.id


def test_output_policy_transforms_once_then_drops_if_still_invalid():
    result = apply_output_policy(
        "请联系 @someone 看链接 https://example.com",
        {"forbid_mentions": True, "forbid_links": True, "failure_strategy": "transform_once_drop"},
        {"remove_mentions": True},
    )

    assert result.allowed is False
    assert result.action == "drop"
    assert result.reason == "命中链接规则"


def test_transform_content_removes_configured_keywords_case_insensitively():
    assert transform_content("这是VIP内容，也是vip内容", {"delete_keywords": ["VIP"]}) == "这是内容，也是内容"


def test_rule_version_copy_and_rollback_create_traceable_versions():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        rule_set = create_rule_set(
            session,
            1,
            RuleSetCreate(name="版本治理", filters={"keyword_whitelist": ["公告"]}, output_checks={"forbid_links": False}),
            "tester",
        )
        copied = copy_rule_set_version(session, 1, rule_set.id, rule_set.active_version_id, "tester")
        copied_version = next(version for version in copied.versions if version.version == 2)
        assert copied_version.status == "draft"
        assert copied_version.version_note == "复制自 v1"
        assert copied_version.filters == {"keyword_whitelist": ["公告"]}

        tightened = create_rule_set_version(
            session,
            1,
            rule_set.id,
            RuleSetVersionCreate(version_note="收紧", filters={"keyword_whitelist": ["活动"]}, output_checks={"forbid_links": True}),
            "tester",
        )
        draft = next(version for version in tightened.versions if version.version == 3)
        published = publish_rule_set_version(session, 1, rule_set.id, draft.id, "tester")
        assert next(version for version in published.versions if version.version == 3).status == "published"

        rolled_back = rollback_rule_set_version(session, 1, rule_set.id, rule_set.active_version_id, "tester")
        active = next(version for version in rolled_back.versions if version.id == rolled_back.active_version_id)

    assert active.version == 4
    assert active.status == "published"
    assert active.version_note == "回滚自 v1"
    assert active.filters == {"keyword_whitelist": ["公告"]}


def test_bound_tasks_show_fixed_and_follow_current_resolution():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        rule_set = create_rule_set(session, 1, RuleSetCreate(name="任务绑定规则"), "tester")
        session.add_all(
            [
                Task(tenant_id=1, name="跟随当前发布", type="group_relay", status="active", type_config={"rule_set_id": rule_set.id}),
                Task(tenant_id=1, name="固定版本", type="group_ai_chat", status="active", type_config={"rule_set_version_id": rule_set.active_version_id}),
                Task(tenant_id=1, name="未绑定", type="message_send", status="active", type_config={}),
            ]
        )
        session.commit()
        rows = list_rule_set_bound_tasks(session, 1, rule_set.id)

    rows_by_name = {row.name: row for row in rows}
    assert set(rows_by_name) == {"跟随当前发布", "固定版本"}
    assert rows_by_name["跟随当前发布"].binding_mode == "follow_current"
    assert rows_by_name["跟随当前发布"].resolved_rule_set_version_id == rule_set.active_version_id
    assert rows_by_name["固定版本"].binding_mode == "fixed_version"
    assert rows_by_name["固定版本"].resolved_rule_set_version_id == rule_set.active_version_id


def test_follow_current_relay_config_solidifies_active_version_for_execution_item():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        rule_set = create_rule_set(session, 1, RuleSetCreate(name="执行固化", task_types=["group_relay"]), "tester")
        task = Task(tenant_id=1, name="监听转发", type="group_relay", status="active", type_config={"rule_set_id": rule_set.id})
        session.add(task)
        session.commit()

        config = effective_relay_config(session, task)
        assert config["rule_binding_mode"] == "follow_current"
        assert config["resolved_rule_set_version_id"] == rule_set.active_version_id

        draft = create_rule_set_version(session, 1, rule_set.id, RuleSetVersionCreate(filters={"keyword_whitelist": ["新规则"]}), "tester")
        draft_version = next(version for version in draft.versions if version.status == "draft")
        published = publish_rule_set_version(session, 1, rule_set.id, draft_version.id, "tester")
        next_config = effective_relay_config(session, task)

    assert published.active_version_id != config["resolved_rule_set_version_id"]
    assert next_config["resolved_rule_set_version_id"] == published.active_version_id


def test_legacy_keyword_rules_do_not_feed_new_rule_center_or_tester():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(ContentKeywordRule(tenant_id=1, keyword="旧口径", match_type="contains", is_active=True))
        session.commit()

        summary = rule_center_summary(session, 1)
        result = preview_rules(session, 1, "这条消息包含旧口径")

    assert summary.keyword_rule_count == 0
    assert summary.keyword_metrics == []
    assert all(item.source != "keyword" for item in summary.items)
    assert result.should_block is False
    assert result.hits == []
    assert result.result == "未命中规则条件"


def test_draft_rule_version_cannot_be_resolved_for_real_task_execution():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()
        rule_set = create_rule_set(session, 1, RuleSetCreate(name="草稿隔离"), "tester")
        draft = create_rule_set_version(session, 1, rule_set.id, RuleSetVersionCreate(filters={"keyword_whitelist": ["草稿"]}), "tester")
        draft_version = next(version for version in draft.versions if version.status == "draft")
        task = Task(tenant_id=1, name="错误绑定草稿", type="group_ai_chat", status="running", type_config={"rule_set_version_id": draft_version.id})
        session.add(task)
        session.commit()

        resolved = bound_rule_version(session, task)

    assert resolved is None
    assert task.last_error == "绑定的规则版本尚未发布"
