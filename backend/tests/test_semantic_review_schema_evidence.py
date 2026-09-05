from types import SimpleNamespace

import pytest

from app.services.task_center.two_stage_generation import (
    TwoStageRealizeError, _parse_semantic_review, _review_realized_content,
)

pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("payload", [None, [], "private response", {"decision": "unexpected private value"}])
def test_invalid_review_exposes_shape_without_response_text(payload):
    with pytest.raises(TwoStageRealizeError, match="semantic_review_schema_invalid") as caught:
        _parse_semantic_review(payload, {})
    shape = caught.value.evidence["schema_validation"]
    assert shape["root_type"] == type(payload).__name__
    assert shape["decision_allowed"] is False
    assert "private" not in str(shape)
    assert "unexpected" not in str(shape)


def test_failed_review_keeps_actual_review_usage_and_original_error():
    def reviewer(*_args, **_kwargs):
        return {"unexpected": "private response"}, 7

    with pytest.raises(TwoStageRealizeError, match="semantic_review_schema_invalid") as caught:
        _review_realized_content(None, 1, {}, plan=SimpleNamespace(brief=None, slot_id="slot-1", reply_preview=""),
            content="明天几点开始？", facts={}, voice={}, reviewer=reviewer, draft_tokens=3)
    assert caught.value.tokens == 10
    assert caught.value.evidence["schema_validation"]["fields"]["decision"] == "missing"
    assert "private" not in str(caught.value.evidence)
