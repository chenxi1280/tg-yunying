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
    for container in containers:
        name = container['Name'].lstrip('/')
        if name != 'tgyunying-backend' and not name.startswith('tgyunying-worker-'):
            continue
        environment = dict(item.split('=', 1) for item in container['Config']['Env'] if '=' in item)
        if container['Config']['Image'] != backend_image or container['Image'] != backend_id:
            raise ValueError('runtime_image_mismatch:' + name)
        state = container['State']
        if environment.get('RELEASE_SHA') != sha or state['Status'] != 'running':
            raise ValueError('runtime_sha_or_state_mismatch:' + name)
        if state.get('Health', {}).get('Status', 'healthy') != 'healthy':
            raise ValueError('runtime_unhealthy:' + name)
        rows.append(name)
    if 'tgyunying-backend' not in rows or not any(n.startswith('tgyunying-worker-') for n in rows):
        raise ValueError('runtime_inventory_missing')
    return rows


def main():
    base, sha, images_json = sys.argv[1:]
    manifest = json.loads(images_json)
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
    print(json.dumps({'sha': sha, 'current': str(current), 'containers': rows,
                      'runtime': 'passed', 'business_evidence': 'unproven'}))


if __name__ == '__main__':
    main()
