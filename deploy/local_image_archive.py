"""Local Docker archive integrity and import; host Python 3.6 compatible."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

COPY_BUFFER_BYTES = 1024 * 1024
COMPRESSION_LEVEL = 1
ARCHIVE_NAME = 'images.tar.gz'
IMAGE_NAMESPACE = 'tgyunying-local'
IMAGE_ENV = {
    'tg-yunying-backend': 'TGYUNYING_BACKEND_IMAGE',
    'tg-yunying-frontend': 'TGYUNYING_FRONTEND_IMAGE',
    'tg-yunying-image-verification-worker': 'TGYUNYING_IMAGE_VERIFICATION_IMAGE',
}
SHA_PATTERN = re.compile(r'[0-9a-f]{40}\Z')
HASH_PATTERN = re.compile(r'[0-9a-f]{64}\Z')
ID_PATTERN = re.compile(r'sha256:[0-9a-f]{64}\Z')


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as content:
        for chunk in iter(lambda: content.read(COPY_BUFFER_BYTES), b''):
            digest.update(chunk)
    return digest.hexdigest()


def image_reference(name, sha, image_id):
    return '{}/{}:{}-{}'.format(IMAGE_NAMESPACE, name, sha, image_id.split(':')[1])


def validate_images(value):
    if value.get('schema_version') != 2 or value.get('status') != 'prepared':
        raise ValueError('local_preparation_not_ready_v2')
    if not SHA_PATTERN.fullmatch(value.get('sha', '')):
        raise ValueError('invalid_candidate_sha')
    if value.get('platform') not in {'linux/amd64', 'linux/arm64'}:
        raise ValueError('invalid_platform')
    images, identities = value.get('images', {}), value.get('image_ids', {})
    if set(images) != set(IMAGE_ENV) or set(identities) != set(IMAGE_ENV):
        raise ValueError('image_set_mismatch')
    for name, identity in identities.items():
        if not isinstance(identity, str) or not ID_PATTERN.fullmatch(identity):
            raise ValueError('invalid_image_id')
        if images[name] != image_reference(name, value['sha'], identity):
            raise ValueError('image_reference_mismatch')
    archive = value.get('archive', {})
    if archive.get('file') != ARCHIVE_NAME:
        raise ValueError('invalid_archive_path')
    if not HASH_PATTERN.fullmatch(archive.get('sha256', '')):
        raise ValueError('invalid_archive_hash')
    if type(archive.get('size')) is not int or archive['size'] <= 0:
        raise ValueError('invalid_archive_size')
    return value


def verify_archive(directory, value):
    archive = directory / value['archive']['file']
    if archive.stat().st_size != value['archive']['size']:
        raise ValueError('image_archive_size_mismatch')
    if file_hash(archive) != value['archive']['sha256']:
        raise ValueError('image_archive_hash_mismatch')
    return archive


def inspect_images(value):
    names = sorted(value['images'])
    command = ['docker', 'image', 'inspect'] + [value['images'][name] for name in names]
    rows = json.loads(subprocess.check_output(command, universal_newlines=True))
    if len(rows) != len(names):
        raise ValueError('loaded_image_count_mismatch')
    for name, row in zip(names, rows):
        if row['Id'] != value['image_ids'][name]:
            raise ValueError('loaded_image_id_mismatch:' + name)
        if row['Os'] + '/' + row['Architecture'] != value['platform']:
            raise ValueError('loaded_image_platform_mismatch:' + name)


def export_archive(directory, value):
    inspect_images(value)
    archive = directory / ARCHIVE_NAME
    command = ['docker', 'image', 'save'] + list(value['images'].values())
    with (directory / 'image-save.log').open('wb') as log:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log) as process:
            with gzip.open(str(archive), 'wb', compresslevel=COMPRESSION_LEVEL) as compressed:
                shutil.copyfileobj(process.stdout, compressed, COPY_BUFFER_BYTES)
            if process.wait():
                raise RuntimeError('docker_save_failed; see image-save.log')
    return {'file': ARCHIVE_NAME, 'size': archive.stat().st_size, 'sha256': file_hash(archive)}


def verify_image_env(path, value):
    entries = dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)
    if entries.get('RELEASE_SHA') != value['sha']:
        raise ValueError('image_env_sha_mismatch')
    for name, variable in IMAGE_ENV.items():
        if entries.get(variable) != value['images'][name]:
            raise ValueError('image_env_reference_mismatch:' + name)


def load_archive(manifest_path, image_env):
    value = validate_images(json.loads(manifest_path.read_text()))
    verify_image_env(image_env, value)
    archive = verify_archive(manifest_path.parent, value)
    subprocess.run(['docker', 'image', 'load', '--input', str(archive)], check=True)
    inspect_images(value)
    print('LOCAL_IMAGES_LOADED=' + value['sha'], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['load', 'verify'])
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--image-env', type=Path, required=True)
    args = parser.parse_args()
    if args.operation == 'load':
        load_archive(args.manifest, args.image_env)
        return
    value = validate_images(json.loads(args.manifest.read_text()))
    verify_image_env(args.image_env, value)
    inspect_images(value)


if __name__ == '__main__':
    main()
