from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.schemas.task_center import ChannelLikeConfig, ChannelViewConfig  # noqa: E402


class ChannelJitterConfigTest(unittest.TestCase):
    def test_view_and_like_jitter_defaults_match_ai_style_ratio(self) -> None:
        base = {"target_channel_id": 1}

        self.assertEqual(ChannelViewConfig(**base).view_count_jitter, 0.2)
        self.assertEqual(ChannelLikeConfig(**base).like_count_jitter, 0.2)

    def test_frontend_exposes_view_and_like_jitter_fields(self) -> None:
        wizard = _read_frontend("TaskCenterWizardSections.tsx")
        channel_config = _read_frontend("TaskCenterChannelConfigSections.tsx")
        view_model = _read_frontend("taskCenterViewModel.ts")

        self.assertIn("ChannelViewTypeConfig", wizard)
        self.assertIn("ChannelLikeTypeConfig", wizard)
        self.assertIn('name="view_count_jitter"', channel_config)
        self.assertIn('name="like_count_jitter"', channel_config)
        self.assertIn("CHANNEL_COUNT_JITTER_DEFAULT = 0.2", view_model)
        self.assertIn("view_count_jitter: CHANNEL_COUNT_JITTER_DEFAULT", view_model)
        self.assertIn("like_count_jitter: CHANNEL_COUNT_JITTER_DEFAULT", view_model)

    def test_frontend_submits_view_and_like_jitter_fields(self) -> None:
        task_center = _read_frontend("TaskCenterView.tsx")
        view_model = _read_frontend("taskCenterViewModel.ts")

        self.assertIn("CHANNEL_COUNT_JITTER_DEFAULT", task_center)
        self.assertIn("view_count_jitter: values.view_count_jitter ?? CHANNEL_COUNT_JITTER_DEFAULT", task_center)
        self.assertIn("like_count_jitter: values.like_count_jitter ?? CHANNEL_COUNT_JITTER_DEFAULT", task_center)
        self.assertIn("'view_count_jitter'", view_model)
        self.assertIn("'like_count_jitter'", view_model)


def _read_frontend(filename: str) -> str:
    path = PROJECT_ROOT / "frontend" / "src" / "app" / "views" / filename
    return path.read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
