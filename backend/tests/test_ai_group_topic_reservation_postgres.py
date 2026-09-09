"""A competing planner must observe the committed topic reservation."""
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AiGroupContentIntent
from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked
from ai_group_content_test_support import _confirm_remote, _freeze, _seed_scope
from tests.test_runtime_retention_protection_postgres import database as database

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def test_competing_planner_preserves_real_topic_capacity(database):
    with Session(database) as seed:
        _seed_scope(seed)
        _freeze(seed, [1, 2, 3])
        intents = list(seed.scalars(select(AiGroupContentIntent).order_by(
            AiGroupContentIntent.normal_text_ordinal)))
        for index, intent in enumerate(intents, 1):
            _confirm_remote(seed, intent, index)
        seed.commit()
    with Session(database) as first, Session(database) as second:
        fourth = _freeze(first, [4])[0]["slot"]
        assert fourth["topic_mode"] == "configured_topic"
        with pytest.raises(RuntimeResourceBlocked, match="ai_group_surface_busy"):
            _freeze(second, [5, 6, 7])
        second.rollback()
        first.commit()

        later = _freeze(second, [5, 6, 7])
        assert all(item["slot"]["topic_mode"] == "group_free_chat" for item in later)
        second.commit()
    with Session(database) as readback:
        assert readback.query(AiGroupContentIntent).count() == 7
        assert readback.query(AiGroupContentIntent).filter_by(
            topic_mode="configured_topic").count() == 1
