from pathlib import Path

import pytest
import yaml


pytestmark = pytest.mark.no_postgres
WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/deploy-production.yml"
COMPOSE_UP = Path(__file__).resolve().parents[2] / "deploy/compose-up.sh"
COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.server.yml"
RELEASE = Path(__file__).resolve().parents[2] / "deploy/release.sh"
BACKEND_DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile.backend"
VERIFICATION_DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile.image-verification-worker"


def test_production_checks_run_complete_backend_partitions_and_frontend_in_parallel() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    no_postgres = jobs["backend-no-postgres-checks"]
    postgres = jobs["backend-postgres-checks"]

    assert no_postgres["strategy"]["matrix"]["shard_index"] == [0, 1, 2]
    assert postgres["strategy"]["matrix"]["shard_index"] == [0, 1]
    assert no_postgres["steps"][-1]["env"]["PYTEST_SHARD_TOTAL"] == "3"
    assert postgres["steps"][-1]["env"]["PYTEST_SHARD_TOTAL"] == "2"
    assert "-m no_postgres -p scripts.pytest_shard" in _combined_run_script(no_postgres)
    assert '-m "not no_postgres" -p scripts.pytest_shard' in _combined_run_script(postgres)
    assert "frontend-checks" in jobs
    expected_needs = {"backend-no-postgres-checks", "backend-postgres-checks", "frontend-checks"}
    assert set(jobs["build-images"]["needs"]) == expected_needs
    assert set(jobs["deploy"]["needs"]) == expected_needs | {"build-images"}


def test_production_images_build_as_three_independent_matrix_entries() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    image_matrix = jobs["build-images"]["strategy"]["matrix"]["include"]

    assert {entry["dockerfile"] for entry in image_matrix} == {
        "Dockerfile.backend",
        "Dockerfile.frontend",
        "Dockerfile.image-verification-worker",
    }
    assert len(image_matrix) == 3
    assert "continue-on-error" not in jobs["build-images"]


@pytest.mark.parametrize("dockerfile", [BACKEND_DOCKERFILE, VERIFICATION_DOCKERFILE])
def test_python_images_cache_dependencies_before_copying_application(dockerfile: Path) -> None:
    content = dockerfile.read_text()
    dependency_copy = "COPY backend/pyproject.toml backend/scripts/install_project_dependencies.py"
    application_copy = "COPY backend/ /app/backend/"

    assert content.index(dependency_copy) < content.index(application_copy)
    assert "--mount=type=cache,target=/root/.cache/pip" in content
    assert "python -m pip install -e . --no-deps --no-build-isolation" in content


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


def test_release_sha_is_injected_into_backend_runtime() -> None:
    release = RELEASE.read_text()
    compose = COMPOSE.read_text()

    assert "RELEASE_SHA=${full_sha}" in release
    assert "RELEASE_SHA: ${RELEASE_SHA:?RELEASE_SHA is required}" in compose


def test_deploy_fences_self_recycling_ocr_worker_and_verifies_runtime_identity() -> None:
    script = COMPOSE_UP.read_text()
    fence = "fence_image_verification_restart"
    stop = 'compose stop "${WORKER_SERVICES[@]}"'
    restore = "restore_image_verification_restart"
    ready = "wait_for_container_ready \\\n    tgyunying-image-verification-worker"
    inventory = "assert_single_image_verification_runtime"

    assert 'docker update --restart=no "$container_id"' in script
    assert 'docker update --restart=unless-stopped "$VERIFICATION_FENCED_CONTAINER_ID"' in script
    assert "assert_fenced_image_verification_stopped" in script
    assert "grep -oE 'docker[-/][0-9a-f]{64}'" in script
    assert script.index(fence, script.index("# ===== Stage A")) < script.index(stop)
    assert script.index(stop) < script.index(restore, script.index(stop))
    assert script.index(ready) < script.index(inventory, script.index(ready))


def _combined_run_script(job: dict) -> str:
    return "\n".join(str(step.get("run") or "") for step in job["steps"])
