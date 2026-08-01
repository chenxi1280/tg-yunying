from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/production-remote-reconcile.yml"
pytestmark = pytest.mark.no_postgres


def test_remote_reconcile_workflow_is_protected_and_release_pinned() -> None:
    source = WORKFLOW.read_text()

    assert "environment: production-silicon-valley" in source
    assert "deployed release SHA does not match workflow SHA" in source
    assert source.index("Verify deployed release and runtime") < source.index(
        "Preview or apply remote evidence"
    )


def test_remote_reconcile_apply_requires_preview_fingerprint_and_approval() -> None:
    source = WORKFLOW.read_text()

    assert "EXPECTED_FINGERPRINT" in source
    assert "--expected-evidence-fingerprint" in source
    assert "--actor" in source
    assert "--approval-ref" in source
    assert "type: choice" in source
    assert "- journal" in source
    assert "STATE=" in source
    assert "remote_confirmed" in source
    assert "remote_absence_proven" in source
    assert "--resolve-conflict" in source
    assert "--expected-action-state-hash" in source
    assert "--expected-attempt-state-hash" in source
