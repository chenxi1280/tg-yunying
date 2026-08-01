from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/reply_ratio_control.py"
WORKFLOW = ROOT / ".github/workflows/production-reply-ratio-control.yml"
pytestmark = pytest.mark.no_postgres


def load_script():
    spec = importlib.util.spec_from_file_location("reply_ratio_control", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("total", "percent", "expected"),
    [(10, 10, 1), (11, 10, 2), (39, 20, 8), (1, 20, 1), (10, 0, 0)],
)
def test_reply_minimum_rounds_up(total: int, percent: int, expected: int) -> None:
    assert load_script().reply_minimum(total, percent) == expected


def test_reply_minimum_rejects_invalid_values() -> None:
    module = load_script()
    with pytest.raises(ValueError):
        module.reply_minimum(0, 10)
    with pytest.raises(ValueError):
        module.reply_minimum(10, 101)


def test_workflow_is_manual_and_verifies_release_before_control() -> None:
    workflow = WORKFLOW.read_text()
    assert "workflow_dispatch:" in workflow
    assert "environment: production-silicon-valley" in workflow
    assert "deployed release SHA does not match workflow SHA" in workflow
    assert workflow.index("Verify deployed release and runtime") < workflow.index("Preview or apply reply ratios")
    assert "REPLY_RATIO_APPLY='${APPLY}'" in workflow
