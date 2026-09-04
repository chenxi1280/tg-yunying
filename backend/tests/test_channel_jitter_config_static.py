from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.schemas.task_center import (  # noqa: E402
    ChannelCommentConfig,
    ChannelLikeConfig,
    ChannelViewConfig,
)


pytestmark = pytest.mark.no_postgres


class ChannelJitterConfigTest(unittest.TestCase):
    def test_unified_view_has_one_ratio_jitter_and_like_keeps_source_jitter(self) -> None:
        base = {"target_channel_id": 1}

        self.assertEqual(ChannelViewConfig(**base).view_count_jitter, 0)
        self.assertEqual(ChannelLikeConfig(**base).like_count_jitter, 0.2)

    def test_unified_view_rejects_double_jitter_or_non_majority_ratio(self) -> None:
        base = {
            "target_channel_id": 1,
            "engagement_contract_version": "unified_engagement_v1",
            "account_group_ids": [7],
        }

        with self.assertRaisesRegex(ValueError, "严格大于 5000"):
            ChannelViewConfig(**base, account_ratio_min_bps=5000)
        with self.assertRaisesRegex(ValueError, "不得叠加 view_count_jitter"):
            ChannelViewConfig(**base, view_count_jitter=0.2)

    def test_unified_task_rejects_empty_account_groups(self) -> None:
        with self.assertRaisesRegex(ValueError, "至少一个账号分组"):
            ChannelViewConfig(
                target_channel_id=1,
                engagement_contract_version="unified_engagement_v1",
            )

    def test_like_and_comment_reject_second_daily_jitter(self) -> None:
        base = {"target_channel_id": 1, "daily_target_jitter_bps": 1500}

        with self.assertRaises(ValueError):
            ChannelLikeConfig(**base)
        with self.assertRaises(ValueError):
            ChannelCommentConfig(**base)

    def test_frontend_exposes_only_like_source_jitter_field(self) -> None:
        wizard = _read_frontend("TaskCenterWizardSections.tsx")
        channel_config = _read_frontend("TaskCenterChannelConfigSections.tsx")
        view_model = _read_frontend("taskCenterViewModel.ts")

        self.assertIn("ChannelViewTypeConfig", wizard)
        self.assertIn("ChannelLikeTypeConfig", wizard)
        self.assertNotIn('name="view_count_jitter"', channel_config)
        self.assertIn('name="like_count_jitter"', channel_config)
        self.assertIn("CHANNEL_COUNT_JITTER_DEFAULT = 0.2", view_model)
        self.assertNotIn("view_count_jitter: CHANNEL_COUNT_JITTER_DEFAULT", view_model)
        self.assertIn("like_count_jitter: CHANNEL_COUNT_JITTER_DEFAULT", view_model)

    def test_frontend_submits_only_like_source_jitter_field(self) -> None:
        task_center = _read_frontend("TaskCenterView.tsx")
        view_model = _read_frontend("taskCenterViewModel.ts")

        self.assertIn("CHANNEL_COUNT_JITTER_DEFAULT", task_center)
        self.assertNotIn("view_count_jitter: values.view_count_jitter ?? CHANNEL_COUNT_JITTER_DEFAULT", task_center)
        self.assertIn("like_count_jitter: values.like_count_jitter ?? CHANNEL_COUNT_JITTER_DEFAULT", task_center)
        self.assertNotIn("'view_count_jitter'", view_model)
        self.assertIn("'like_count_jitter'", view_model)

    def test_frontend_supports_all_available_reaction_scope(self) -> None:
        task_center = _read_frontend("TaskCenterView.tsx")
        channel_config = _read_frontend("TaskCenterChannelConfigSections.tsx")
        view_model = _read_frontend("taskCenterViewModel.ts")

        self.assertIn('name="reaction_scope"', channel_config)
        self.assertIn("value: 'all_available'", channel_config)
        self.assertIn("各表情数量不设固定比例", channel_config)
        self.assertIn("reaction_scope: 'all_available'", view_model)
        self.assertIn("reaction_scope: values.reaction_scope", task_center)


def _read_frontend(filename: str) -> str:
    path = PROJECT_ROOT / "frontend" / "src" / "app" / "views" / filename
    return path.read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
