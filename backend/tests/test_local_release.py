import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.no_postgres
ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('local_release', ROOT / 'deploy/local_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64


def manifest():
    return {'schema_version': 1, 'status': 'prepared', 'sha': SHA,
            'platform': 'linux/amd64', 'tests': ['tests/example.py'],
            'logs': {'test.log': 'digest'},
            'images': {name: f'{release.REGISTRY}/{name}@{DIGEST}' for name in release.IMAGES}}


@pytest.mark.parametrize('change', [
    {'status': 'preparing'}, {'sha': 'HEAD'}, {'images': {}}, {'tests': []},
    {'platform': 'darwin/arm64'}, {'logs': {}},
])
def test_invalid_preparation_is_rejected(change):
    with pytest.raises(ValueError):
        release.validate_manifest({**manifest(), **change})


@pytest.mark.parametrize('reference', ['untrusted/image@' + DIGEST,
                                      release.REGISTRY + '/tg-yunying-backend:latest'])
def test_only_project_digest_is_deployable(reference):
    value = manifest()
    value['images']['tg-yunying-backend'] = reference
    with pytest.raises(ValueError):
        release.validate_manifest(value)


def git(repository, *args):
    return subprocess.check_output(['git', *args], cwd=repository, text=True).strip()


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, 'init', '-q', '-b', 'release')
    (tmp_path / 'tracked').write_text('committed')
    git(tmp_path, 'add', 'tracked')
    git(tmp_path, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '-qm', 'candidate')
    return tmp_path


def test_snapshot_excludes_dirty_files_even_when_caller_is_release(repository):
    sha = git(repository, 'rev-parse', 'HEAD')
    (repository / 'tracked').write_text('dirty')
    (repository / 'untracked').write_text('private')
    with release.source_snapshot(repository, sha) as source:
        assert (source / 'tracked').read_text() == 'committed'
        assert not (source / 'untracked').exists()
        assert git(source, 'branch', '--show-current') == 'release'
    assert (repository / 'tracked').read_text() == 'dirty'


def test_remote_refs_must_both_equal_candidate(repository):
    sha = git(repository, 'rev-parse', 'HEAD')
    git(repository, 'remote', 'add', 'origin', str(repository))
    with pytest.raises(ValueError, match='remote_master_release'):
        release.require_frozen_remote(repository, sha)
    git(repository, 'branch', 'master', sha)
    release.require_frozen_remote(repository, sha)
    with pytest.raises(ValueError, match='remote_master_release'):
        release.require_frozen_remote(repository, SHA)


def test_stage_failure_is_logged_and_not_swallowed(tmp_path):
    log = tmp_path / 'stage.log'
    with pytest.raises(RuntimeError, match='exit=7'):
        release.execute(['bash', '-c', 'echo broken; exit 7'], cwd=tmp_path, log=log)
    assert 'broken' in log.read_text()


def test_timeout_terminates_test_process(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        release.execute(['sleep', '20'], cwd=tmp_path,
                        log=tmp_path / 'timeout.log', timeout=0.1)


def test_changed_log_rejected(tmp_path):
    (tmp_path / 'test.log').write_text('changed')
    with pytest.raises(ValueError, match='log_changed'):
        release.verify_logs(tmp_path, manifest())


def test_test_failure_never_creates_ready_manifest(repository, monkeypatch):
    options = SimpleNamespace(ref='HEAD', platform='linux/amd64', test=['bad'],
                              python='python3', output=repository / 'evidence')
    monkeypatch.setattr(release, 'require_commands', lambda names: None)
    original = subprocess.run

    def run(command, **kwargs):
        if command[0] == 'docker':
            return SimpleNamespace(returncode=0)
        return original(command, **kwargs)

    monkeypatch.setattr(release.subprocess, 'run', run)
    monkeypatch.setattr(release, 'run_checks', lambda *args: (_ for _ in ()).throw(
        RuntimeError('test_failed')))
    with pytest.raises(RuntimeError, match='test_failed'):
        release.prepare(options, repository)
    assert json.loads((options.output / 'prepared-release.json').read_text())['status'] == 'preparing'


@pytest.mark.parametrize('host', ['host;touch /tmp/unwanted', '-oProxyCommand=bad'])
def test_destination_cannot_inject_remote_shell(host):
    with pytest.raises(ValueError):
        release.validate_destination(SimpleNamespace(host=host, user='root', base_dir='/data/app'))


def test_deployment_failure_is_not_replayed(repository, monkeypatch):
    value = manifest()
    value['sha'] = git(repository, 'rev-parse', 'HEAD')
    path = repository / 'manifest.json'
    path.write_text(json.dumps(value))
    options = SimpleNamespace(manifest=path, host='production', user='root', base_dir='/data/app')
    for name in ['require_commands', 'verify_logs', 'require_frozen_remote', 'require_target_platform']:
        monkeypatch.setattr(release, name, lambda *args: None)
    monkeypatch.setenv('GHCR_USERNAME', 'test')
    monkeypatch.setenv('GHCR_TOKEN', 'test')
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise RuntimeError('install_connection_unknown')

    monkeypatch.setattr(release, 'execute', fail)
    with pytest.raises(RuntimeError, match='install_connection_unknown'):
        release.deploy(options, repository)
    assert json.loads((repository / 'deployment.json').read_text())['status'] == 'deployment_unproven'
    with pytest.raises(FileExistsError):
        release.deploy(options, repository)
    assert len(calls) == 1


def test_production_actions_are_inactive():
    for name in ['prepare-production.yml', 'deploy-production.yml']:
        assert not (ROOT / '.github/workflows' / name).exists()
        assert (ROOT / '.github/workflows' / (name + '.disabled')).is_file()


def test_portable_install_timeout_returns_unknown_without_retry(tmp_path):
    calls = tmp_path / 'calls'
    command = ['python3', str(ROOT / 'deploy/run_with_timeout.py'), '1',
               'bash', '-c', 'echo invoked >> "$1"; sleep 20', 'test', str(calls)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=4)
    assert result.returncode == 124
    assert calls.read_text().splitlines() == ['invoked']
    assert 'inspect production' in result.stderr


def test_readback_rejects_container_with_correct_sha_but_wrong_image(monkeypatch):
    module_spec = importlib.util.spec_from_file_location(
        'local_release_readback', ROOT / 'deploy/local_release_readback.py')
    reader = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(reader)
    container = {'Name': '/tgyunying-backend', 'Config': {
        'Env': ['RELEASE_SHA=' + SHA], 'Image': 'wrong-image'},
        'State': {'Status': 'running'}}
    monkeypatch.setattr(reader, 'output', lambda command: (
        'container-id' if command[1] == 'ps' else json.dumps([container])))
    with pytest.raises(ValueError, match='runtime_image_mismatch'):
        reader.verify_containers(SHA, 'expected-image')
