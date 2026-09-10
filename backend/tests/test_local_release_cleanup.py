import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.no_postgres
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy'))
spec = importlib.util.spec_from_file_location('cleanup_tools', ROOT / 'deploy/local_release_cleanup.py')
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)
from local_image_archive import IMAGE_ENV, file_hash, image_reference

OLD_ID = 'sha256:' + 'a' * 64
NEW_ID = 'sha256:' + 'b' * 64


def make_manifest(directory, sha='a' * 40, identity=OLD_ID):
    directory.mkdir(exist_ok=True)
    archive = directory / 'images.tar.gz'
    archive.write_bytes(b'old release image archive')
    value = {'schema_version': 2, 'status': 'prepared', 'sha': sha, 'platform': 'linux/amd64',
             'images': {name: image_reference(name, sha, identity) for name in IMAGE_ENV},
             'image_ids': dict.fromkeys(IMAGE_ENV, identity),
             'archive': {'file': archive.name, 'size': archive.stat().st_size,
                         'sha256': file_hash(archive)}}
    (directory / 'prepared-release.json').write_text(json.dumps(value))
    return value


def mock_docker(monkeypatch, actual=OLD_ID, used=()):
    calls = []
    monkeypatch.setattr(cleanup, 'image_id', lambda ref: actual)
    monkeypatch.setattr(cleanup, 'in_use_ids', lambda: set(used))
    monkeypatch.setattr(cleanup.subprocess, 'run', lambda command, **kwargs: calls.append(command))
    return calls


@pytest.mark.parametrize('actual, used, expected', [
    (None, (), 'already_absent'),
    (NEW_ID, (), 'reference_changed_preserved'),
    (OLD_ID, (OLD_ID,), 'in_use_or_current_preserved'),
])
def test_old_image_absent_changed_or_referenced_is_not_deleted(monkeypatch, actual, used, expected):
    calls = mock_docker(monkeypatch, actual, used)
    result = cleanup.remove_images({'backend': {'reference': 'old:tag', 'id': OLD_ID}},
                                   {'image_ids': {'backend': NEW_ID}})
    assert result[0]['state'] == expected
    assert calls == []


def test_identical_current_image_is_preserved(monkeypatch):
    calls = mock_docker(monkeypatch)
    result = cleanup.remove_images({'backend': {'reference': 'old:tag', 'id': OLD_ID}},
                                   {'image_ids': {'backend': OLD_ID}})
    assert result[0]['state'] == 'in_use_or_current_preserved'
    assert calls == []


def test_unused_previous_reference_is_removed_without_force(monkeypatch):
    calls = mock_docker(monkeypatch)
    cleanup.remove_images({'backend': {'reference': 'old:tag', 'id': OLD_ID}},
                          {'image_ids': {'backend': NEW_ID}})
    assert calls == [['docker', 'image', 'rm', 'old:tag']]


def test_corrupt_old_archive_prevents_cleanup_mutations(tmp_path, monkeypatch):
    value = make_manifest(tmp_path)
    (tmp_path / 'images.tar.gz').write_bytes(b'changed')
    calls = mock_docker(monkeypatch)
    with pytest.raises(ValueError, match='archive_size_mismatch'):
        cleanup.clean_previous({'directory': str(tmp_path), 'images': {},
                                'archive_manifest': value}, {'image_ids': {}})
    assert calls == [] and (tmp_path / 'images.tar.gz').exists()


def test_success_cleans_only_previous_local_release_and_keeps_current(tmp_path, monkeypatch):
    old, new, untouched = [tmp_path / name for name in ('old', 'new', 'not-deployed')]
    old_value, new_value = make_manifest(old), make_manifest(new, 'b' * 40, NEW_ID)
    make_manifest(untouched)
    mock_docker(monkeypatch)
    receipt = {'host': 'production', 'base_dir': '/data/app', 'started_at': '2026-09-10T01:00:00Z'}
    cleanup.clean_local(old, old_value, receipt)
    result = cleanup.clean_local(new, new_value, {**receipt, 'started_at': '2026-09-10T02:00:00Z'})
    assert result['status'] == 'cleanup_passed'
    assert not (old / 'images.tar.gz').exists()
    assert (old / 'prepared-release.json').exists()
    assert (new / 'images.tar.gz').exists() and (untouched / 'images.tar.gz').exists()


def test_older_completion_cannot_delete_newer_release(tmp_path, monkeypatch):
    old, new = tmp_path / 'old', tmp_path / 'new'
    old_value, new_value = make_manifest(old), make_manifest(new, 'b' * 40, NEW_ID)
    calls = mock_docker(monkeypatch)
    receipt = {'host': 'production', 'base_dir': '/data/app', 'started_at': '2026-09-10T02:00:00Z'}
    cleanup.clean_local(new, new_value, receipt)
    result = cleanup.clean_local(old, old_value, {**receipt, 'started_at': '2026-09-10T01:00:00Z'})
    assert result['status'] == 'newer_or_same_deployment_preserved'
    assert calls == [] and (new / 'images.tar.gz').exists()


def test_server_runtime_failure_keeps_archives(tmp_path, monkeypatch):
    current = tmp_path / 'release'
    value = make_manifest(current)
    (current / 'local-images.json').write_text(json.dumps(value))
    (tmp_path / 'current').symlink_to(current)
    monkeypatch.setattr(cleanup, 'verify_release', lambda *args: (_ for _ in ()).throw(
        RuntimeError('runtime_failed')))
    calls = mock_docker(monkeypatch)
    with pytest.raises(RuntimeError, match='runtime_failed'):
        cleanup.clean_server(tmp_path, current)
    assert calls == [] and (current / 'images.tar.gz').exists()


def test_capture_legacy_release_uses_only_exact_recorded_references(tmp_path, monkeypatch):
    old, new = tmp_path / 'old', tmp_path / 'new'
    old.mkdir(); new.mkdir()
    reference = 'ghcr.io/chenxi1280/tg-yunying-backend:old-sha'
    (old / '.image.env').write_text('TGYUNYING_BACKEND_IMAGE=' + reference + '\n')
    (tmp_path / 'current').symlink_to(old)
    mock_docker(monkeypatch)
    cleanup.capture_previous(tmp_path, new)
    value = json.loads((new / 'previous-images.json').read_text())
    assert value['images'] == {'tg-yunying-backend': {'reference': reference, 'id': OLD_ID}}
    assert value['archive_manifest'] is None


def test_server_cleanup_occurs_after_all_install_checks_inside_original_lock():
    script = (ROOT / 'deploy/server-install-release.sh').read_text()
    assert script.index('if ! flock -n 8; then') < script.index('local_release_cleanup.py" capture')
    assert script.index('restart-antigravity-provider-slots.sh') < script.index('local_release_cleanup.py" clean')
    assert script.index('local_release_cleanup.py" clean') < script.index('echo "Release ${RELEASE_ID} is live"')


def test_successful_server_cleanup_removes_previous_and_current_packages(tmp_path, monkeypatch):
    old, new = tmp_path / 'old', tmp_path / 'new'
    old_value, new_value = make_manifest(old), make_manifest(new, 'b' * 40, NEW_ID)
    (new / 'local-images.json').write_text(json.dumps(new_value))
    previous = {'directory': str(old), 'archive_manifest': old_value,
                'images': {'backend': {'reference': 'old:tag', 'id': OLD_ID}}}
    (new / 'previous-images.json').write_text(json.dumps(previous))
    (tmp_path / 'current').symlink_to(new)
    calls = mock_docker(monkeypatch)
    monkeypatch.setattr(cleanup, 'verify_release', lambda *args: None)
    monkeypatch.setattr(cleanup, 'inspect_images', lambda value: None)
    cleanup.clean_server(tmp_path, new)
    assert calls == [['docker', 'image', 'rm', 'old:tag']]
    assert not (old / 'images.tar.gz').exists() and not (new / 'images.tar.gz').exists()
    assert (new / 'local-images.json').exists()
    report = json.loads((new / 'image-cleanup.json').read_text())
    assert report['runtime'] == 'passed' and report['status'] == 'cleanup_passed'


def test_local_cleanup_failure_keeps_successful_runtime_receipt(tmp_path, monkeypatch):
    import local_release as driver

    monkeypatch.setattr(driver, 'clean_local', lambda *args: (_ for _ in ()).throw(
        RuntimeError('cleanup_failed')))
    with pytest.raises(RuntimeError, match='cleanup_failed'):
        driver.finalize_deployment(tmp_path, {}, {'sha': 'a' * 40})
    receipt = json.loads((tmp_path / 'deployment.json').read_text())
    assert receipt['status'] == 'release_passed' and receipt['cleanup'] == 'failed'
