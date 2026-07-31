from pathlib import Path

import pytest
import yaml


pytestmark = pytest.mark.no_postgres
WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/deploy-production.yml"


def test_production_checks_run_complete_backend_partitions_and_frontend_in_parallel() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    backend = jobs["backend-checks"]
    partitions = backend["strategy"]["matrix"]["include"]
    no_postgres_paths = {
        partition["pytest_paths"]
        for partition in partitions
        if partition["pytest_marker"] == "no_postgres"
    }

    assert no_postgres_paths == {
        "tests/test_[a-b]*.py",
        "tests/test_[c-d]*.py",
        "tests/test_[e-l]*.py",
        "tests/test_[m-p]*.py",
        "tests/test_[q-s]*.py",
        "tests/test_[t-z]*.py",
    }
    assert partitions[-1]["pytest_marker"] == "not no_postgres"
    assert partitions[-1]["pytest_paths"] == "tests"
    assert '${{ matrix.pytest_paths }}' in _combined_run_script(backend)
    assert "frontend-checks" in jobs
    assert set(jobs["build-images"]["needs"]) == {"backend-checks", "frontend-checks"}


def _combined_run_script(job: dict) -> str:
    return "\n".join(str(step.get("run") or "") for step in job["steps"])
