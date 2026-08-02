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
    for container in (
        "tgyunying-worker-planner",
        "tgyunying-worker-ai-generation",
        "tgyunying-worker-dispatcher-1",
        "tgyunying-worker-dispatcher-2",
        "tgyunying-worker-listener",
        "tgyunying-worker-recovery",
    ):
        assert container in workflow


def test_script_exposes_unknown_remote_reconciliation_evidence() -> None:
    module = load_script()

    assert callable(module.unknown_remote_snapshot)
    assert callable(module._unknown_remote_row)
    assert callable(module.terminal_action_snapshot)
    assert callable(module._terminal_action_row)


def test_terminal_diagnostics_expose_failure_stage_and_detail() -> None:
    module = load_script()
    action = type("ActionStub", (), {
        "result": {
            "error_code": "reply_target_missing",
            "error_message": "引用目标不存在或当前账号不可引用",
            "validation_stage": "ai_reply_target",
            "generation_stage": "reply_target_validation",
        },
    })()

    assert module._remote_result_contract(action) == {
        "error_code": "reply_target_missing",
        "error_message": "引用目标不存在或当前账号不可引用",
        "validation_stage": "ai_reply_target",
        "generation_stage": "reply_target_validation",
    }


def test_comment_binding_contract_exposes_superseding_action() -> None:
    module = load_script()
    action = type("ActionStub", (), {"id": "stale"})()
    current = type("CurrentStub", (), {
        "id": "current",
        "status": "pending",
        "created_at": None,
        "scheduled_at": None,
    })()
    obligation = type("ObligationStub", (), {
        "id": "obligation-1",
        "status": "pending",
        "action_attempt_no": 2,
        "current_action_id": current.id,
    })()

    assert module._comment_binding_contract(action, obligation, current) == {
        "obligation_id": "obligation-1",
        "obligation_status": "pending",
        "obligation_attempt_no": 2,
        "current_action_id": "current",
        "is_current_action": False,
        "current_action_status": "pending",
        "current_action_created_at": None,
        "current_action_scheduled_at": None,
    }
