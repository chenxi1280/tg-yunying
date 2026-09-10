"""Local image verification failures must precede all worker changes."""
import json
import os
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.no_postgres
ROOT = Path(__file__).resolve().parents[2]
SHA = 'a' * 40
IMAGE_ID = 'b' * 64
DOCKER_STUB = '''#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$DOCKER_CALLS_FILE"
if [[ "$1 $2" == "network inspect" ]]; then
  printf '172.25.0.1\\n'
  exit 0
fi
if [[ "$1 $2" == "image inspect" ]]; then
  echo 'local image missing' >&2
  exit 42
fi
echo 'unexpected Docker operation' >&2
exit 98
'''


def _runtime_env(tmp_path):
    docker = tmp_path / 'docker'
    docker.write_text(DOCKER_STUB)
    docker.chmod(0o755)
    names = {'tg-yunying-backend': 'TGYUNYING_BACKEND_IMAGE',
             'tg-yunying-frontend': 'TGYUNYING_FRONTEND_IMAGE',
             'tg-yunying-image-verification-worker': 'TGYUNYING_IMAGE_VERIFICATION_IMAGE'}
    images = {name: f'tgyunying-local/{name}:{SHA}-{IMAGE_ID}' for name in names}
    manifest = {'schema_version': 2, 'status': 'prepared', 'sha': SHA, 'platform': 'linux/amd64',
                'images': images, 'image_ids': dict.fromkeys(names, 'sha256:' + IMAGE_ID),
                'archive': {'file': 'images.tar.gz', 'sha256': 'c' * 64, 'size': 1}}
    (tmp_path / 'local-images.json').write_text(json.dumps(manifest))
    env = tmp_path / '.env'
    env.write_text('\n'.join([
        'DATABASE_URL=postgresql://unreachable/test', 'REDIS_URL=redis://unreachable/0',
        'SESSION_SECRET_KEY=test', 'ADMIN_BOOTSTRAP_PASSWORD=test',
        'CORS_ORIGINS=http://example.invalid', 'PUBLIC_APP_BASE_URL=http://example.invalid',
        'IMAGE_VERIFICATION_CONTRACT_ENABLED=false', 'ENABLE_EMBEDDED_WORKER=false']))
    image_env = tmp_path / '.image.env'
    image_env.write_text('RELEASE_SHA=' + SHA + '\n' + '\n'.join(
        key + '=' + images[name] for name, key in names.items()))
    return {**os.environ, 'PATH': f'{tmp_path}{os.pathsep}{os.environ["PATH"]}',
            'APP_DIR': str(tmp_path), 'BASE_DIR': str(tmp_path), 'ENV_FILE': str(env),
            'IMAGE_ENV_FILE': str(image_env), 'DOCKER_CALLS_FILE': str(tmp_path / 'calls')}


def test_missing_imported_image_fails_without_pull_or_worker_fence(tmp_path):
    result = subprocess.run(['bash', str(ROOT / 'deploy/compose-up.sh')],
        env=_runtime_env(tmp_path), capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert 'local image missing' in result.stderr
    calls = (tmp_path / 'calls').read_text().splitlines()
    assert len(calls) == 2
    assert calls[0].startswith('network inspect ')
    assert calls[1].startswith('image inspect ')
