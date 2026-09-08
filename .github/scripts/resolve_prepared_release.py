"""Resolve a successful preparation run for the exact frozen release SHA."""
from __future__ import annotations

import io
import json
import os
import subprocess
import zipfile

from prepared_release_manifest import SCHEMA_VERSION, validate_manifest

PREPARE_WORKFLOW = "prepare-production.yml"
PREPARE_PATH = f".github/workflows/{PREPARE_WORKFLOW}"
MAX_MANIFEST_BYTES = 16_384
IMAGE_OUTPUTS = {
    "tg-yunying-backend": "backend_image",
    "tg-yunying-frontend": "frontend_image",
    "tg-yunying-image-verification-worker": "verification_image",
}


def github_api(path: str) -> bytes:
    return subprocess.run(
        ["gh", "api", path], check=True, stdout=subprocess.PIPE, timeout=60,
    ).stdout


def require_trusted_run(run: dict, *, repository: str, sha: str) -> None:
    expected = {
        "head_sha": sha, "head_branch": "master", "path": PREPARE_PATH,
        "status": "completed", "conclusion": "success",
    }
    for key, value in expected.items():
        if run.get(key) != value:
            raise ValueError(f"untrusted_preparation_run:{key}")
    if run.get("event") not in {"push", "workflow_dispatch"}:
        raise ValueError("untrusted_preparation_event")
    if run.get("head_repository", {}).get("full_name") != repository:
        raise ValueError("untrusted_preparation_repository")


def select_run(runs: list[dict], *, repository: str, sha: str) -> dict:
    for run in sorted(runs, key=lambda row: row["id"], reverse=True):
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            continue
        require_trusted_run(run, repository=repository, sha=sha)
        return run
    raise ValueError(
        f"prepared_release_not_ready:{sha}; wait for Prepare Production on master "
        "to succeed before dispatching Deploy Production",
    )


def read_manifest_archive(content: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) != 1 or entries[0].filename != "prepared-release.json":
            raise ValueError("prepared_release_archive_members_invalid")
        if entries[0].file_size > MAX_MANIFEST_BYTES:
            raise ValueError("prepared_release_manifest_too_large")
        return json.loads(archive.read(entries[0]))


def resolve(*, repository: str, sha: str, api=github_api) -> dict:
    base = f"repos/{repository}/actions"
    listing = json.loads(api(
        f"{base}/workflows/{PREPARE_WORKFLOW}/runs?head_sha={sha}&per_page=100",
    ))
    run = select_run(listing["workflow_runs"], repository=repository, sha=sha)
    expected = {
        "schema_version": SCHEMA_VERSION, "repository": repository, "sha": sha,
        "run_id": run["id"], "run_attempt": run["run_attempt"],
    }
    name = f"prepared-release-{sha}-{run['run_attempt']}"
    artifacts = json.loads(api(f"{base}/runs/{run['id']}/artifacts?per_page=100"))
    matches = [row for row in artifacts["artifacts"] if row["name"] == name]
    if len(matches) != 1 or matches[0].get("expired") is not False:
        raise ValueError("prepared_release_artifact_missing_duplicate_or_expired")
    artifact = matches[0]
    value = read_manifest_archive(api(f"{base}/artifacts/{artifact['id']}/zip"))
    validate_manifest(value, expected)
    # A rerun may have started while the artifact was being downloaded.
    fresh = json.loads(api(f"{base}/runs/{run['id']}"))
    require_trusted_run(fresh, repository=repository, sha=sha)
    if fresh["run_attempt"] != expected["run_attempt"]:
        raise ValueError("prepared_release_run_attempt_changed")
    return {**value, "artifact_id": artifact["id"], "run_url": run["html_url"]}


def main() -> None:
    result = resolve(repository=os.environ["GITHUB_REPOSITORY"], sha=os.environ["GITHUB_SHA"])
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for image, key in IMAGE_OUTPUTS.items():
            output.write(f"{key}={result['images'][image]}\n")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write(
            f"Prepared release `{result['sha']}` from {result['run_url']} "
            f"attempt {result['run_attempt']}, artifact {result['artifact_id']}.\n",
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
