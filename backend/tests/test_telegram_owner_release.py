import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.no_postgres
DEPLOY = Path(__file__).resolve().parents[2] / 'deploy'


def readback():
    spec = importlib.util.spec_from_file_location('owner_release_readback', DEPLOY / 'local_release_readback.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inventory():
    return [{'Name': name, 'Image': 'image-id',
             'Config': {'Image': 'image', 'Env': ['RELEASE_SHA=sha',
                        'TELEGRAM_OWNER_MODE=' + ('server' if name.endswith('owner') else 'client'),
                        'TELEGRAM_OWNER_SOCKET=/run/tgyunying-telegram/owner.sock']},
             'State': {'Status': 'running', 'Health': {'Status': 'healthy'}},
             'Mounts': [{'Type': 'bind', 'Source': '/shared/owner',
                         'Destination': '/run/tgyunying-telegram', 'RW': True}]}
            for name in ('tgyunying-backend', 'tgyunying-worker-dispatcher', 'tgyunying-telegram-owner')]


@pytest.mark.parametrize('change', ('none', 'missing', 'other_mount', 'local_client'))
def test_readback_requires_one_shared_owner(monkeypatch, change):
    module, rows = readback(), inventory()
    if change == 'missing':
        rows.pop()
    if change == 'other_mount':
        rows[1]['Mounts'][0]['Source'] = '/other/owner'
    if change == 'local_client':
        rows[1]['Config']['Env'][1] = 'TELEGRAM_OWNER_MODE=local'
    monkeypatch.setattr(module, 'output', lambda command: 'id' if command[1] == 'ps' else json.dumps(rows))
    if change == 'none':
        assert len(module.verify_containers('sha', 'image', 'image-id')) == 3
    else:
        with pytest.raises(ValueError, match='owner'):
            module.verify_containers('sha', 'image', 'image-id')


@pytest.mark.parametrize('drift', (False, True))
def test_readback_checks_frozen_direct_egress_on_every_caller(monkeypatch, drift):
    module, rows = readback(), inventory()
    policy = {'TELEGRAM_DIRECT_EGRESS_REGION': 'sv', 'TELEGRAM_DIRECT_EGRESS_IP': '8.8.8.8'}
    for row in rows:
        row['Config']['Env'].extend(key + '=' + value for key, value in policy.items())
    if drift:
        rows[1]['Config']['Env'][-1] = 'TELEGRAM_DIRECT_EGRESS_IP=1.1.1.1'
    monkeypatch.setattr(module, 'output', lambda command: 'id' if command[1] == 'ps' else json.dumps(rows))
    if drift:
        with pytest.raises(ValueError, match='direct_egress_policy_mismatch'):
            module.verify_containers('sha', 'image', 'image-id', expected_egress=policy)
    else:
        assert len(module.verify_containers('sha', 'image', 'image-id', expected_egress=policy)) == 3


@pytest.mark.parametrize('failure', ('none', 'ps', 'stop', 'inspect', 'alive'))
def test_old_owner_process_exit_required(tmp_path, failure):
    docker = tmp_path / 'docker'
    docker.write_text('''#!/usr/bin/env bash
set -eu
if [[ "$1" == "$FAILURE" ]]; then exit 42; fi
if [[ "$1" == ps ]]; then echo old-owner; fi
if [[ "$1" == inspect ]]; then
 if [[ "$FAILURE" == alive ]]; then echo 'old-owner running 10'; else echo 'old-owner exited 0'; fi
fi
''')
    docker.chmod(0o755)
    script = 'set -euo pipefail\nsource "$SCRIPT_DIR/worker-cutover.sh"\nstop_release_connection_owner\necho exit_confirmed'
    result = subprocess.run(['bash', '-c', script], text=True, capture_output=True, timeout=5,
                            env={**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
                                 'SCRIPT_DIR': str(DEPLOY), 'FAILURE': failure})
    assert (result.returncode == 0) == (failure == 'none')
    assert ('exit_confirmed' in result.stdout) == (failure == 'none')
