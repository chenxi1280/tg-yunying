#!/usr/bin/env python3
"""Prepare immutable images locally and deploy without GitHub Actions."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import tempfile

REGISTRY = 'ghcr.io/chenxi1280'
IMAGES = {
    'tg-yunying-backend': ('Dockerfile.backend', 'TGYUNYING_BACKEND_IMAGE'),
    'tg-yunying-frontend': ('Dockerfile.frontend', 'TGYUNYING_FRONTEND_IMAGE'),
    'tg-yunying-image-verification-worker': (
        'Dockerfile.image-verification-worker', 'TGYUNYING_IMAGE_VERIFICATION_IMAGE'),
}
TEST_TIMEOUT_SECONDS = 60
SHA_PATTERN = re.compile(r'[0-9a-f]{40}\Z')
DIGEST_PATTERN = re.compile(r'sha256:[0-9a-f]{64}\Z')


def capture(command, *, cwd=None):
    return subprocess.check_output(command, cwd=cwd, text=True).strip()


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def execute(command, *, cwd, log, timeout=None, env=None):
    print(f'STAGE_LOG={log}', flush=True)
    with log.open('wb') as output:
        process = subprocess.Popen(command, cwd=cwd, stdout=output,
                                   stderr=subprocess.STDOUT, env=env, start_new_session=True)
        try:
            status = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
    if status:
        raise RuntimeError(f'stage_failed:exit={status}; log={log}')
    return hashlib.sha256(log.read_bytes()).hexdigest()


@contextmanager
def source_snapshot(repository, sha):
    with tempfile.TemporaryDirectory(prefix='tgyunying-local-release-') as directory:
        source = Path(directory) / 'source'
        subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout',
                        str(repository), str(source)], check=True)
        subprocess.run(['git', 'checkout', '--quiet', '-B', 'release', sha],
                       cwd=source, check=True)
        yield source


def validate_manifest(value):
    if value.get('schema_version') != 1 or value.get('status') != 'prepared':
        raise ValueError('local_preparation_not_ready')
    if not SHA_PATTERN.fullmatch(value.get('sha', '')):
        raise ValueError('invalid_candidate_sha')
    if value.get('platform') not in {'linux/amd64', 'linux/arm64'}:
        raise ValueError('invalid_platform')
    if not value.get('tests') or not value.get('logs'):
        raise ValueError('missing_local_test_evidence')
    images = value.get('images', {})
    if set(images) != set(IMAGES):
        raise ValueError('image_set_mismatch')
    for name, reference in images.items():
        prefix = f'{REGISTRY}/{name}@'
        if not isinstance(reference, str) or not reference.startswith(prefix):
            raise ValueError('image_repository_mismatch')
        if not DIGEST_PATTERN.fullmatch(reference[len(prefix):]):
            raise ValueError('invalid_image_digest')
    return value


def require_commands(names):
    for name in names:
        if not shutil.which(name):
            raise RuntimeError(f'missing_command:{name}')


def run_checks(source, options):
    logs = {}
    environment = {**os.environ, 'PYTHONPATH': str(source / 'backend')}
    for index, selection in enumerate(options.test):
        command = [options.python, '-m', 'pytest', '-q', *shlex.split(selection)]
        logs[f'test-{index}.log'] = execute(command, cwd=source / 'backend',
            log=options.output / f'test-{index}.log', timeout=TEST_TIMEOUT_SECONDS,
            env=environment)
    for name, command in [('frontend-install', ['npm', 'ci']),
                          ('frontend-build', ['npm', 'run', 'build'])]:
        logs[f'{name}.log'] = execute(command, cwd=source / 'frontend',
            log=options.output / f'{name}.log', env={**os.environ, 'VITE_API_BASE': '/api'})
    # Build inputs must remain the committed source after test/build execution.
    subprocess.run(['git', 'diff', '--exit-code', 'HEAD'], cwd=source, check=True)
    return logs


def build_images(source, options, sha):
    images, logs = {}, {}
    for name, (dockerfile, _) in IMAGES.items():
        metadata = options.output / f'{name}.json'
        command = ['docker', 'buildx', 'build', '--platform', options.platform,
            '--file', dockerfile, '--tag', f'{REGISTRY}/{name}:{sha}', '--push',
            '--metadata-file', str(metadata)]
        if name == 'tg-yunying-frontend':
            command += ['--build-arg', 'VITE_API_BASE=/api']
        log_name = f'{name}.log'
        logs[log_name] = execute([*command, '.'], cwd=source,
                                log=options.output / log_name)
        digest = json.loads(metadata.read_text())['containerimage.digest']
        if not isinstance(digest, str) or not DIGEST_PATTERN.fullmatch(digest):
            raise ValueError(f'invalid_build_digest:{name}')
        images[name] = f'{REGISTRY}/{name}@{digest}'
    return images, logs


def prepare(options, repository):
    require_commands(['docker', 'npm', options.python])
    subprocess.run(['docker', 'info'], stdout=subprocess.DEVNULL, check=True)
    subprocess.run(['docker', 'buildx', 'version'], check=True)
    sha = capture(['git', 'rev-parse', f'{options.ref}^{{commit}}'], cwd=repository)
    options.output.mkdir(parents=True, exist_ok=False)
    value = {'schema_version': 1, 'sha': sha, 'platform': options.platform,
             'tests': options.test, 'status': 'preparing',
             'created_at': datetime.now(timezone.utc).isoformat()}
    save(options.output / 'prepared-release.json', value)
    with source_snapshot(repository, sha) as source:
        logs = run_checks(source, options)
    # Fresh snapshot excludes generated and ignored test/build artifacts.
    with source_snapshot(repository, sha) as source:
        images, build_logs = build_images(source, options, sha)
    value.update(status='prepared', images=images, logs={**logs, **build_logs})
    save(options.output / 'prepared-release.json', validate_manifest(value))
    print(f"PREPARED_SHA={sha}", flush=True)


def require_frozen_remote(repository, sha):
    lines = capture(['git', 'ls-remote', 'origin', 'refs/heads/master',
                     'refs/heads/release'], cwd=repository).splitlines()
    refs = dict(line.split()[::-1] for line in lines)
    expected = dict.fromkeys(['refs/heads/master', 'refs/heads/release'], sha)
    if refs != expected:
        raise ValueError('remote_master_release_candidate_mismatch')


def verify_logs(directory, manifest):
    for name, digest in manifest['logs'].items():
        if Path(name).name != name:
            raise ValueError('invalid_log_path')
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f'test_or_build_log_changed:{name}')


def validate_destination(options):
    for value in (options.host, options.user):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value):
            raise ValueError('invalid_ssh_destination')
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', options.base_dir):
        raise ValueError('invalid_remote_base_directory')


def require_target_platform(options, platform):
    actual = capture(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                      f'{options.user}@{options.host}', 'uname -sm'])
    expected = {'linux/amd64': 'Linux x86_64', 'linux/arm64': 'Linux aarch64'}
    if actual != expected[platform]:
        raise ValueError('target_platform_mismatch')


def deploy(options, repository):
    require_commands(['ssh', 'scp', 'python3'])
    validate_destination(options)
    manifest = validate_manifest(json.loads(options.manifest.read_text()))
    directory = options.manifest.parent
    verify_logs(directory, manifest)
    require_frozen_remote(repository, manifest['sha'])
    require_target_platform(options, manifest['platform'])
    for name in ['GHCR_USERNAME', 'GHCR_TOKEN']:
        if not os.environ.get(name):
            raise RuntimeError(f'missing_environment:{name}')
    receipt_path = directory / 'deployment.json'
    receipt = {'sha': manifest['sha'], 'status': 'deploying',
               'host': options.host, 'business_evidence': 'unproven'}
    # Exclusive creation prevents a repeated invocation from replaying installation.
    with receipt_path.open('x') as output:
        json.dump(receipt, output)
    with source_snapshot(repository, manifest['sha']) as source:
        environment = {**os.environ, 'POST_DEPLOY_CHECKS_ENABLED': '1'}
        environment.update({IMAGES[name][1]: ref for name, ref in manifest['images'].items()})
        try:
            execute(['bash', 'deploy/release.sh', '--host', options.host,
                     '--user', options.user, '--base-dir', options.base_dir],
                    cwd=source, log=directory / 'deploy.log', env=environment)
            verify_runtime(source, options, manifest)
        except Exception:
            save(receipt_path, {**receipt, 'status': 'deployment_unproven'})
            raise
    save(receipt_path, {**receipt, 'status': 'release_passed'})
    print(f'RELEASE_RESULT={receipt_path}', flush=True)


def verify_runtime(source, options, manifest):
    command = shlex.join(['python3', '-', options.base_dir, manifest['sha'],
                          json.dumps(manifest['images'])])
    with (source / 'deploy/local_release_readback.py').open('rb') as script:
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes',
            f'{options.user}@{options.host}', command], stdin=script,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    (options.manifest.parent / 'runtime.json').write_bytes(result.stdout)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    preparation = sub.add_parser('prepare')
    preparation.add_argument('--ref', required=True)
    preparation.add_argument('--platform', choices=['linux/amd64', 'linux/arm64'], required=True)
    preparation.add_argument('--python', required=True, help='Absolute backend venv Python path')
    preparation.add_argument('--test', action='append', required=True,
                             help='pytest arguments for one batch, repeatable; 60s per batch')
    preparation.add_argument('--output', type=Path, required=True)
    installation = sub.add_parser('deploy')
    installation.add_argument('--manifest', type=Path, required=True)
    installation.add_argument('--host', required=True, help='Configured SSH host alias')
    installation.add_argument('--user', default='root')
    installation.add_argument('--base-dir', default='/data/tgyunying')
    return parser.parse_args()


def main():
    options = arguments()
    repository = Path(capture(['git', 'rev-parse', '--show-toplevel']))
    if options.operation == 'prepare':
        options.output = options.output.resolve()
        options.python = str(Path(options.python).resolve())
        prepare(options, repository)
    else:
        options.manifest = options.manifest.resolve()
        deploy(options, repository)


if __name__ == '__main__':
    main()
