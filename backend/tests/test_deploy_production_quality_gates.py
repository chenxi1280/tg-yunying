from pathlib import Path

import pytest
import yaml


pytestmark = pytest.mark.no_postgres
WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/deploy-production.yml"
COMPOSE_UP = Path(__file__).resolve().parents[2] / "deploy/compose-up.sh"


def test_production_checks_run_complete_backend_partitions_and_frontend_in_parallel() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    backend = jobs["backend-checks"]
    markers = backend["strategy"]["matrix"]["pytest_marker"]

    assert set(markers) == {"no_postgres", "not no_postgres"}
    assert 'pytest -q -m "${{ matrix.pytest_marker }}"' in _combined_run_script(backend)
    assert "frontend-checks" in jobs
    assert set(jobs["build-images"]["needs"]) == {"backend-checks", "frontend-checks"}


def test_deploy_prunes_only_dangling_images_before_pull() -> None:
    script = COMPOSE_UP.read_text()

    assert "docker image prune -f" in script
    assert "docker image prune -af" not in script


def test_deploy_pulls_large_runtime_images_sequentially() -> None:
    script = COMPOSE_UP.read_text()

    backend_pull = 'compose pull "${BACKEND_SERVICES[@]}"'
    verification_pull = "compose pull image-verification-worker"
    frontend_pull = 'docker pull "$TGYUNYING_FRONTEND_IMAGE"'

    assert backend_pull in script
    assert verification_pull in script
    assert frontend_pull in script
    assert script.index(backend_pull) < script.index(verification_pull)
    assert script.index(verification_pull) < script.index(frontend_pull)
    assert 'compose pull "${RUNTIME_SERVICES[@]}"' not in script


def _combined_run_script(job: dict) -> str:
    return "\n".join(str(step.get("run") or "") for step in job["steps"])
