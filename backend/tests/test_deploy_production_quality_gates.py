from pathlib import Path

import pytest
import yaml


pytestmark = pytest.mark.no_postgres
WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/deploy-production.yml"


def test_production_checks_run_complete_backend_partitions_and_frontend_in_parallel() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    backend = jobs["backend-checks"]
    markers = backend["strategy"]["matrix"]["pytest_marker"]

    assert set(markers) == {"no_postgres", "not no_postgres"}
    assert 'pytest -q -m "${{ matrix.pytest_marker }}"' in _combined_run_script(backend)
    assert "frontend-checks" in jobs
    assert set(jobs["build-images"]["needs"]) == {"backend-checks", "frontend-checks"}


def _combined_run_script(job: dict) -> str:
    return "\n".join(str(step.get("run") or "") for step in job["steps"])
