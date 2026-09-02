from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models import (
    ChannelCommentGroundingAssignment,
    ChannelCommentGroundingEvaluation,
    ChannelCommentGroundingSnapshot,
    ChannelMessageSourceRevision,
)
from app.schemas.task_center import TaskDetailOut
from app.services.task_center.channel_comment_grounding_extractor import (
    extract_grounding_facts,
)
from app.services.task_center.channel_comment_grounding_snapshot import (
    assignment_eligible_variants,
)


pytestmark = pytest.mark.no_postgres


def test_extractor_preserves_spans_negation_multiple_teachers_and_time() -> None:
    source = (
        "不是糖糖老师，今日主推妮妮老师\n"
        "妮妮老师 黑丝 下午可约\n"
        "圆圆老师 水疗"
    )

    facts = extract_grounding_facts(
        source,
        datetime(2030, 8, 1, 2, 0, tzinfo=timezone.utc),
        content_route="general",
    )

    candidates = facts["teacher_candidates_json"]
    assert any(row["normalized_name"] == "糖糖" and row["negated"] for row in candidates)
    assert {row["normalized_name"] for row in candidates if not row["negated"]} == {
        "妮妮", "圆圆",
    }
    assert facts["teacher_state"] == "multiple_supported"
    for row in [*candidates, *facts["aspect_evidence_json"]]:
        start, end = row["source_start"], row["source_end"]
        assert source[start:end] == row["source_text"]
    timed = [row for row in facts["aspect_evidence_json"] if row["valid_until"]]
    assert timed
    assert all(row["valid_until"].startswith("2030-08-01T23:59:59") for row in timed)
    water = next(row for row in facts["aspect_evidence_json"] if row["source_text"] == "水疗")
    teacher = next(row for row in candidates if row["candidate_id"] == water["teacher_candidate_id"])
    assert teacher["normalized_name"] == "圆圆"


def test_temporal_evidence_is_not_assigned_past_latest_safe_send() -> None:
    facts = extract_grounding_facts(
        "今晚活动",
        datetime(2030, 8, 1, 2, 0, tzinfo=timezone.utc),
        content_route="general",
    )

    eligible = assignment_eligible_variants(
        facts,
        latest_safe_send_at=datetime(2030, 8, 2, 2, 0, tzinfo=timezone.utc),
    )

    assert eligible
    assert {row["primary_evidence_id"] for row in eligible} == {"e-2"}


def test_extractor_does_not_turn_links_into_grounding_evidence() -> None:
    facts = extract_grounding_facts(
        "https://example.com @contact",
        datetime(2030, 8, 1, tzinfo=timezone.utc),
        content_route="general",
    )

    assert facts["source_state"] == "insufficient"
    assert facts["groundable_capacity_count"] == 0


def test_teacher_identity_is_groundable_without_inventing_an_attribute() -> None:
    facts = extract_grounding_facts(
        "糖糖老师",
        datetime(2030, 8, 1, tzinfo=timezone.utc),
        content_route="general",
    )

    assert facts["source_state"] == "minimal"
    evidence = facts["aspect_evidence_json"]
    assert [(row["aspect_code"], row["source_text"]) for row in evidence] == [
        ("teacher_identity", "糖糖"),
    ]
    assert facts["groundable_capacity_count"] == 4


@pytest.mark.parametrize(
    ("source", "expected_name"),
    [
        ("我觉得圆圆老师今天水疗不错", "圆圆"),
        ("昨天朋友推荐圆圆老师，身材高挑", "圆圆"),
        ("今天的妮妮老师可以预约", "妮妮"),
    ],
)
def test_teacher_suffix_excludes_natural_language_introducers(
    source: str,
    expected_name: str,
) -> None:
    facts = extract_grounding_facts(
        source,
        datetime(2030, 8, 1, tzinfo=timezone.utc),
        content_route="general",
    )

    names = [
        row["normalized_name"]
        for row in facts["teacher_candidates_json"]
        if not row["negated"]
    ]
    assert names == [expected_name]


def test_multiple_teacher_evidence_does_not_cross_line_blocks_or_conflicts() -> None:
    facts = extract_grounding_facts(
        "不是糖糖老师，糖糖老师 黑丝\n今晚可约\n妮妮老师 水疗",
        datetime(2030, 8, 1, tzinfo=timezone.utc),
        content_route="general",
    )

    assert facts["teacher_state"] == "conflict"
    timed = next(
        row for row in facts["aspect_evidence_json"] if row["source_text"] == "今晚"
    )
    assert timed["teacher_candidate_id"] == ""
    assert all(
        row["teacher_name"] != "糖糖老师"
        for row in facts["semantic_variant_units_json"]
    )


def test_grounding_schema_contains_append_only_identity_columns() -> None:
    snapshot_columns = set(ChannelCommentGroundingSnapshot.__table__.columns.keys())
    evaluation_columns = set(ChannelCommentGroundingEvaluation.__table__.columns.keys())
    assignment_columns = set(ChannelCommentGroundingAssignment.__table__.columns.keys())
    source_columns = set(ChannelMessageSourceRevision.__table__.columns.keys())

    assert {
        "source_revision_id", "comment_grounding_revision", "teacher_candidates_json",
        "aspect_evidence_json", "semantic_variant_units_json", "extraction_audit_json",
    } <= snapshot_columns
    assert {
        "generation_attempt_id", "candidate_content_hash", "claim_results_json",
        "primary_aspect_result", "reply_relation_result", "final_result",
    } <= evaluation_columns
    assert {
        "grounding_snapshot_id", "teacher_candidate_id", "primary_evidence_id",
        "secondary_evidence_id", "relation_kind",
    } <= assignment_columns
    assert {
        "channel_target_id", "telegram_edit_date", "source_published_at_fact_id",
        "source_type", "source_length", "captured_length", "truncation_state",
    } <= source_columns
    assert {
        "channel_comment_discussion", "channel_comment_grounding",
    } <= set(TaskDetailOut.model_fields)


def test_0195_migration_follows_discussion_contract_and_creates_grounding_tables() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations/versions/0195_channel_comment_grounding_snapshot.py"
    )
    source = path.read_text()

    assert 'revision = "0195_comment_grounding_snapshot"' in source
    assert 'down_revision = "0194_channel_comment_discussion"' in source
    assert '"channel_comment_grounding_snapshots"' in source
    assert '"channel_comment_grounding_evaluations"' in source
