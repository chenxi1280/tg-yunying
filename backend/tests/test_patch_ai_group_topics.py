from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

from scripts import patch_ai_group_topics


pytestmark = pytest.mark.no_postgres


def _snapshot() -> dict:
    tasks = [
        SimpleNamespace(
            id=f"task-{group_id}",
            tenant_id=1,
            config_revision=1,
            type_config={"target_group_id": group_id},
        )
        for group_id in patch_ai_group_topics.APPROVED_GROUP_TOPICS
    ]
    groups = [
        SimpleNamespace(id=group_id, topic_direction="日常讨论、活动答疑")
        for group_id in patch_ai_group_topics.APPROVED_GROUP_TOPICS
    ]
    return patch_ai_group_topics._snapshot(tasks, groups)


def test_topic_manifest_targets_only_the_approved_seven_groups() -> None:
    assert set(patch_ai_group_topics.APPROVED_GROUP_TOPICS) == {
        5998,
        5363,
        3848,
        2818,
        2821,
        5828,
        5996,
    }


def test_apply_requires_matching_snapshot_and_independent_approval() -> None:
    snapshot = _snapshot()
    args = argparse.Namespace(
        expected_fingerprint=patch_ai_group_topics._fingerprint(snapshot),
        expected_task_count=7,
        expected_group_count=7,
        requested_by="operator-a",
        approved_by="operator-b",
        approval_ref="approval-20260827",
    )

    patch_ai_group_topics._validate_apply(args, snapshot)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("expected_fingerprint", "wrong", "expected_fingerprint_mismatch"),
        ("expected_task_count", 6, "expected_task_count_mismatch"),
        ("expected_group_count", 6, "expected_group_count_mismatch"),
        ("approved_by", "operator-a", "requester_and_approver_must_differ"),
    ],
)
def test_apply_rejects_stale_or_unapproved_inputs(field: str, value: object, error: str) -> None:
    snapshot = _snapshot()
    values = {
        "expected_fingerprint": patch_ai_group_topics._fingerprint(snapshot),
        "expected_task_count": 7,
        "expected_group_count": 7,
        "requested_by": "operator-a",
        "approved_by": "operator-b",
        "approval_ref": "approval-20260827",
    }
    values[field] = value

    with pytest.raises(ValueError, match=error):
        patch_ai_group_topics._validate_apply(argparse.Namespace(**values), snapshot)
