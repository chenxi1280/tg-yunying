from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, OperationTarget, RuleSet, RuleSetVersion, Task, Tenant, TgAccount, TgGroup
from app.services.operations_center import operation_metrics_summary, relay_attribution_report, rule_center_summary
from app.services.operations_center_risk import risk_control_details
from app.services.reports import build_overview


@contextmanager
def _sqlite_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _seed_unknown_action(session: Session) -> None:
    occurred_at = datetime(2026, 6, 25, 10, 0, 0)
    session.add_all(
        [
            Tenant(id=1, name="默认运营空间"),
            TgAccount(id=11, tenant_id=1, phone_masked="+861***0011", display_name="发送号", status="在线"),
            TgGroup(id=101, tenant_id=1, title="目标群", tg_peer_id="-100101"),
            OperationTarget(id=101, tenant_id=1, target_type="group", tg_peer_id="-100101", title="目标群"),
            RuleSet(id=21, tenant_id=1, name="转发规则", status="active"),
            RuleSetVersion(id=31, tenant_id=1, rule_set_id=21, version=1, status="published"),
            Task(id="relay-task", tenant_id=1, name="转发任务", type="group_relay", status="running"),
            Action(
                id="relay-unknown",
                tenant_id=1,
                task_id="relay-task",
                task_type="group_relay",
                action_type="send_message",
                account_id=11,
                status="unknown_after_send",
                payload={
                    "relay_batch_id": "batch-1",
                    "relay_event_id": "event-1",
                    "original_text": "来源消息",
                    "message_text": "转发消息",
                    "group_id": 101,
                    "operation_target_id": 101,
                    "rule_set_id": 21,
                    "rule_set_version_id": 31,
                },
                result={"error_code": "unknown_after_send", "error_message": "worker lost after gateway call"},
                scheduled_at=occurred_at,
                executed_at=occurred_at,
                created_at=occurred_at,
            ),
        ]
    )
    session.commit()


def _metric_value(rows: list[object], key: str) -> object:
    for row in rows:
        if getattr(row, "key", "") == key:
            return getattr(row, "value", None)
    raise AssertionError(f"metric not found: {key}")


def test_operation_surfaces_count_unknown_after_send_as_unresolved_failure() -> None:
    with _sqlite_session() as session:
        _seed_unknown_action(session)

        overview = build_overview(session, 1)
        metrics = operation_metrics_summary(session, 1)
        risk_details = risk_control_details(session, 1)
        rules = rule_center_summary(session, 1)
        attribution = relay_attribution_report(session, 1)

    execution = rules.execution_metrics[0]
    target_metric = rules.target_metrics[0]
    trend = next(item for item in rules.trend_metrics if item.action_count)
    cross = rules.cross_metrics[0]

    assert overview["queue"]["failed_actions"] == 1
    assert any("结果未知" in item["title"] for item in overview["risks"])
    assert _metric_value(metrics.failures, "failures.actions") == 1
    assert metrics.failure_details[0].status == "unknown_after_send"
    assert any(item.status == "unknown_after_send" and item.category == "结果未知" for item in risk_details)
    assert execution.failed_count == 1
    assert execution.pending_count == 0
    assert target_metric.failed_count == 1
    assert target_metric.pending_count == 0
    assert trend.failed_count == 1
    assert trend.pending_count == 0
    assert cross.failed_count == 1
    assert cross.pending_count == 0
    assert attribution.rows[0].failed_count == 1
    assert attribution.rows[0].pending_count == 0
