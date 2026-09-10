import gzip
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.no_postgres
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('archive_tools', ROOT / 'deploy/local_image_archive.py')
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)
SHA = 'a' * 40
IDENTITY = 'sha256:' + 'b' * 64


@pytest.fixture
def prepared(tmp_path):
    package = tmp_path / 'images.tar.gz'
    with gzip.open(package, 'wb') as stream:
        stream.write(b'archive boundary fixture')
    value = {'schema_version': 2, 'status': 'prepared', 'sha': SHA,
             'platform': 'linux/amd64', 'image_ids': dict.fromkeys(archive.IMAGE_ENV, IDENTITY),
             'images': {name: archive.image_reference(name, SHA, IDENTITY)
                        for name in archive.IMAGE_ENV},
             'archive': {'file': package.name, 'sha256': archive.file_hash(package),
                         'size': package.stat().st_size}}
    manifest = tmp_path / 'local-images.json'
    manifest.write_text(json.dumps(value))
    env = tmp_path / '.image.env'
    env.write_text('RELEASE_SHA=' + SHA + '\n' + '\n'.join(
        key + '=' + value['images'][name] for name, key in archive.IMAGE_ENV.items()))
    return manifest, env, value


def forbid_docker(*args, **kwargs):
    pytest.fail('Docker must not run before archive and identity validation')


def test_corrupt_archive_fails_before_docker(prepared, monkeypatch):
    manifest, env, value = prepared
    package = manifest.parent / 'images.tar.gz'
    package.write_bytes(b'x' * value['archive']['size'])
    monkeypatch.setattr(archive.subprocess, 'run', forbid_docker)
    with pytest.raises(ValueError, match='archive_hash_mismatch'):
        archive.load_archive(manifest, env)


def test_truncated_archive_fails_before_docker(prepared, monkeypatch):
    manifest, env, _ = prepared
    (manifest.parent / 'images.tar.gz').write_bytes(b'x')
    monkeypatch.setattr(archive.subprocess, 'run', forbid_docker)
    with pytest.raises(ValueError, match='archive_size_mismatch'):
        archive.load_archive(manifest, env)


def test_manifest_cannot_load_for_different_source(prepared, monkeypatch):
    manifest, env, _ = prepared
    env.write_text(env.read_text().replace(SHA, 'c' * 40))
    monkeypatch.setattr(archive.subprocess, 'run', forbid_docker)
    with pytest.raises(ValueError, match='env_sha_mismatch'):
        archive.load_archive(manifest, env)


def test_load_error_keeps_package_and_does_not_replay(prepared, monkeypatch):
    manifest, env, _ = prepared
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(9, command)

    monkeypatch.setattr(archive.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        archive.load_archive(manifest, env)
    assert len(calls) == 1
    assert (manifest.parent / 'images.tar.gz').exists()


@pytest.mark.parametrize('row, error', [
    ({'Id': 'sha256:' + 'c' * 64, 'Os': 'linux', 'Architecture': 'amd64'}, 'id_mismatch'),
    ({'Id': IDENTITY, 'Os': 'linux', 'Architecture': 'arm64'}, 'platform_mismatch'),
])
def test_loaded_identity_and_architecture_are_checked(prepared, monkeypatch, row, error):
    manifest, env, value = prepared
    monkeypatch.setattr(archive.subprocess, 'run', lambda *args, **kwargs: None)
    monkeypatch.setattr(archive.subprocess, 'check_output', lambda *args, **kwargs: json.dumps([row] * 3))
    with pytest.raises(ValueError, match=error):
        archive.load_archive(manifest, env)
    assert (manifest.parent / 'images.tar.gz').exists()


def test_successful_import_retains_manifest_and_removes_only_uploaded_archive(prepared, monkeypatch):
    manifest, env, _ = prepared
    calls = []
    unrelated = manifest.parent / 'unrelated.tar.gz'
    unrelated.write_bytes(b'keep')
    monkeypatch.setattr(archive.subprocess, 'run', lambda command, **kwargs: calls.append(command))
    row = {'Id': IDENTITY, 'Os': 'linux', 'Architecture': 'amd64'}
    monkeypatch.setattr(archive.subprocess, 'check_output', lambda *args, **kwargs: json.dumps([row] * 3))
    archive.load_archive(manifest, env)
    assert len(calls) == 1 and calls[0][:3] == ['docker', 'image', 'load']
    assert manifest.exists() and unrelated.exists()
    assert not (manifest.parent / 'images.tar.gz').exists()


@pytest.mark.parametrize('change', [
    {'schema_version': 1}, {'image_ids': {}},
    {'archive': {'file': '../images.tar.gz', 'size': 1, 'sha256': 'c' * 64}},
])
def test_legacy_or_unsafe_archive_manifest_is_rejected(prepared, change):
    with pytest.raises(ValueError):
        archive.validate_images({**prepared[2], **change})


def test_server_import_runs_inside_lock_before_any_runtime_install():
    script = (ROOT / 'deploy/server-install-release.sh').read_text()
    lock = script.index('if ! flock -n 8; then')
    load = script.index('local_image_archive.py" load')
    install = script.index('bash "${RELEASE_DIR}/deploy/compose-up.sh"')
    assert lock < load < install


def test_source_and_image_uploads_precede_single_install():
    script = (ROOT / 'deploy/release.sh').read_text()
    upload = script.index('Uploading local Docker archive')
    install = script.index('run_with_timeout.py')
    assert upload < install
    assert 'GHCR_TOKEN' not in script and 'IMAGE_NAMESPACE' not in script
    assert script.count('run_with_timeout.py') == 1
