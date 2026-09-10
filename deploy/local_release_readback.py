"""Read-only production verification, compatible with host Python 3.6."""
import json
from pathlib import Path
import subprocess
import sys


def output(command):
    return subprocess.check_output(command, universal_newlines=True).strip()


def verify_containers(sha, backend_image, backend_id):
    ids = output(['docker', 'ps', '-aq', '--filter', 'name=^/tgyunying-']).split()
    containers = json.loads(output(['docker', 'inspect'] + ids)) if ids else []
    rows = []
    owner_mounts = set()
    for container in containers:
        name = container['Name'].lstrip('/')
        if name not in ('tgyunying-backend', 'tgyunying-telegram-owner') and not name.startswith('tgyunying-worker-'):
            continue
        environment = dict(item.split('=', 1) for item in container['Config']['Env'] if '=' in item)
        if container['Config']['Image'] != backend_image or container['Image'] != backend_id:
            raise ValueError('runtime_image_mismatch:' + name)
        state = container['State']
        if environment.get('RELEASE_SHA') != sha or state['Status'] != 'running':
            raise ValueError('runtime_sha_or_state_mismatch:' + name)
        if state.get('Health', {}).get('Status', 'healthy') != 'healthy':
            raise ValueError('runtime_unhealthy:' + name)
        expected_mode = 'server' if name == 'tgyunying-telegram-owner' else 'client'
        if environment.get('TELEGRAM_OWNER_MODE') != expected_mode:
            raise ValueError('runtime_telegram_owner_mode_mismatch:' + name)
        owner_mounts.add(_owner_mount(container, environment, name))
        rows.append(name)
    if len(owner_mounts) != 1:
        raise ValueError('runtime_telegram_owner_mount_mismatch')
    if 'tgyunying-telegram-owner' not in rows:
        raise ValueError('runtime_telegram_owner_missing')
    if 'tgyunying-backend' not in rows or not any(n.startswith('tgyunying-worker-') for n in rows):
        raise ValueError('runtime_inventory_missing')
    return rows


def _owner_mount(container, environment, name):
    if environment.get('TELEGRAM_OWNER_SOCKET') != '/run/tgyunying-telegram/owner.sock':
        raise ValueError('runtime_telegram_owner_socket_mismatch:' + name)
    mounts = [row for row in container.get('Mounts', [])
              if row['Destination'] == '/run/tgyunying-telegram']
    if len(mounts) != 1 or not mounts[0].get('RW') or mounts[0]['Type'] != 'bind':
        raise ValueError('runtime_telegram_owner_mount_missing:' + name)
    return mounts[0]['Source']


def verify_release(base, sha, manifest):
    images = manifest['images']
    current = (Path(base) / 'current').resolve(strict=True)
    values = dict(line.split('=', 1) for line in (current / '.image.env').read_text().splitlines()
                  if '=' in line)
    if values.get('RELEASE_SHA') != sha:
        raise ValueError('current_sha_mismatch')
    names = {'tg-yunying-backend': 'TGYUNYING_BACKEND_IMAGE',
             'tg-yunying-frontend': 'TGYUNYING_FRONTEND_IMAGE',
             'tg-yunying-image-verification-worker': 'TGYUNYING_IMAGE_VERIFICATION_IMAGE'}
    for name, key in names.items():
        if values.get(key) != images[name]:
            raise ValueError('current_image_mismatch:' + name)
    rows = verify_containers(sha, images['tg-yunying-backend'],
                             manifest['image_ids']['tg-yunying-backend'])
    output(['docker', 'exec', 'tgyunying-backend', 'python', '-m',
            'scripts.manage_shared_dispatch_contract', 'verify-active'])
    return {'sha': sha, 'current': str(current), 'containers': rows,
            'runtime': 'passed', 'business_evidence': 'unproven'}


def main():
    base, sha, images_json = sys.argv[1:]
    print(json.dumps(verify_release(base, sha, json.loads(images_json))))


if __name__ == '__main__':
    main()
