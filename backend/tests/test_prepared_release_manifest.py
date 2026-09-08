from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import zipfile

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / ".github/scripts"
sys.path.insert(0, str(SCRIPTS))
from prepared_release_manifest import IMAGE_NAMES, REGISTRY, assemble, validate_manifest
from resolve_prepared_release import PREPARE_PATH, read_manifest_archive, resolve

pytestmark = pytest.mark.no_postgres
SHA = "a" * 40
IDENTITY = {
    "schema_version": 1, "repository": "chenxi1280/tg-yunying", "sha": SHA,
    "run_id": 123, "run_attempt": 2,
}


def _manifest():
    return {**IDENTITY, "images": {
        name: f"{REGISTRY}/{name}@sha256:{'b' * 64}" for name in IMAGE_NAMES
    }}


def _archive(value, *, filename="prepared-release.json"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(filename, json.dumps(value))
    return buffer.getvalue()


def _run(**changes):
    return {
        "id": 123, "run_attempt": 2, "head_sha": SHA, "head_branch": "master",
        "path": PREPARE_PATH, "status": "completed", "conclusion": "success",
        "event": "push", "head_repository": {"full_name": IDENTITY["repository"]},
        "html_url": "https://github.com/chenxi1280/tg-yunying/actions/runs/123",
        **changes,
    }


def _api(*, run=None, manifest=None, expired=False, fresh=None):
    run = run or _run()
    def api(path):
        if "/workflows/" in path:
            value = {"workflow_runs": [run]}
        elif path.endswith("/zip"):
            return _archive(manifest or _manifest())
        elif "/artifacts?" in path:
            value = {"artifacts": [{"id": 456, "name": f"prepared-release-{SHA}-2", "expired": expired}]}
        else:
            value = fresh or run
        return json.dumps(value).encode()
    return api


def test_resolver_binds_exact_run_attempt_and_digests():
    result = resolve(repository=IDENTITY["repository"], sha=SHA, api=_api())
    assert result["artifact_id"] == 456
    assert result["run_attempt"] == 2
    assert result["images"] == _manifest()["images"]


@pytest.mark.parametrize("changes", [
    {"head_sha": "c" * 40}, {"head_branch": "release"}, {"path": "another.yml"},
    {"conclusion": "failure"}, {"status": "in_progress"}, {"event": "pull_request"},
    {"head_repository": {"full_name": "untrusted/fork"}},
])
def test_resolver_rejects_wrong_source_and_unsuccessful_run(changes):
    with pytest.raises(ValueError):
        resolve(repository=IDENTITY["repository"], sha=SHA, api=_api(run=_run(**changes)))


@pytest.mark.parametrize("key,value", [("sha", "c" * 40), ("run_id", 124), ("run_attempt", 1)])
def test_resolver_rejects_artifact_from_another_identity(key, value):
    with pytest.raises(ValueError, match="identity_mismatch"):
        resolve(repository=IDENTITY["repository"], sha=SHA, api=_api(manifest={**_manifest(), key: value}))


def test_resolver_rejects_rerun_race_and_expired_artifact():
    for api in (_api(expired=True), _api(fresh=_run(run_attempt=3))):
        with pytest.raises(ValueError):
            resolve(repository=IDENTITY["repository"], sha=SHA, api=api)


def test_manifest_requires_all_three_exact_repositories_and_digests():
    for invalid in ("ghcr.io/other/image@sha256:" + "b" * 64, "ghcr.io/chenxi1280/tg-yunying-backend:latest"):
        value = _manifest()
        value["images"][IMAGE_NAMES[0]] = invalid
        with pytest.raises(ValueError):
            validate_manifest(value, IDENTITY)
    value = _manifest()
    value["images"].pop(IMAGE_NAMES[0])
    with pytest.raises(ValueError):
        validate_manifest(value, IDENTITY)


def test_assembly_requires_complete_unique_same_attempt_images(tmp_path):
    for name, reference in _manifest()["images"].items():
        (tmp_path / f"{name}.json").write_text(json.dumps({**IDENTITY, "images": {name: reference}}))
    assert assemble(tmp_path, IDENTITY) == _manifest()
    path = tmp_path / f"{IMAGE_NAMES[0]}.json"
    value = json.loads(path.read_text())
    path.write_text(json.dumps({**value, "run_attempt": 1}))
    with pytest.raises(ValueError, match="built_image_identity_mismatch"):
        assemble(tmp_path, IDENTITY)


def test_archive_rejects_path_traversal_without_extracting():
    with pytest.raises(ValueError, match="archive_members"):
        read_manifest_archive(_archive(_manifest(), filename="../prepared-release.json"))
