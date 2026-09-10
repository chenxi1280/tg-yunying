"""Clean only the previous application's release artifacts after runtime verification."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess

from local_image_archive import IMAGE_ENV, inspect_images, validate_images, verify_archive
from local_release_readback import verify_release


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    temporary.replace(path)


def image_id(reference):
    result = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', reference],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if result.returncode and 'No such image:' in result.stderr:
        return None
    if result.returncode:
        raise RuntimeError('image_inspection_failed:' + result.stderr)
    return result.stdout.strip()


def in_use_ids():
    identifiers = subprocess.check_output(['docker', 'ps', '-aq'], universal_newlines=True).split()
    if not identifiers:
        return set()
    output = subprocess.check_output(['docker', 'inspect', '--format', '{{.Image}}'] + identifiers,
                                     universal_newlines=True)
    return set(output.split())


def previous_images(env):
    result = {}
    for name, key in IMAGE_ENV.items():
        reference = env.get(key)
        if not reference:
            continue
        prefixes = ('tgyunying-local/' + name + ':',
                    'ghcr.io/chenxi1280/' + name + ':', 'ghcr.io/chenxi1280/' + name + '@')
        if not reference.startswith(prefixes):
            raise ValueError('unexpected_previous_image_repository:' + name)
        result[name] = {'reference': reference, 'id': image_id(reference)}
    return result


def capture_previous(base, destination):
    current_link = base / 'current'
    snapshot = {'images': {}, 'directory': None, 'archive_manifest': None}
    if current_link.exists():
        current = current_link.resolve()
        env = dict(line.split('=', 1) for line in (current / '.image.env').read_text().splitlines()
                   if '=' in line)
        snapshot.update(directory=str(current), images=previous_images(env))
        manifest = current / 'local-images.json'
        if manifest.exists():
            snapshot['archive_manifest'] = validate_images(json.loads(manifest.read_text()))
    save(destination / 'previous-images.json', snapshot)


def remove_images(previous, current):
    protected = set(current['image_ids'].values())
    result = []
    for row in previous.values():
        reference, identity = row['reference'], row['id']
        actual = image_id(reference)
        if actual is None:
            state = 'already_absent'
        elif actual != identity:
            state = 'reference_changed_preserved'
        elif identity in protected or identity in in_use_ids():
            state = 'in_use_or_current_preserved'
        else:
            subprocess.run(['docker', 'image', 'rm', reference], check=True)
            state = 'removed'
        result.append({'reference': reference, 'state': state})
    return result


def remove_archive(directory, value):
    path = directory / value['archive']['file']
    if not path.exists():
        return 'already_absent'
    if path.is_symlink():
        raise ValueError('archive_symlink_refused')
    verify_archive(directory, value).unlink()
    return 'removed'


def clean_previous(snapshot, current):
    old_manifest = snapshot.get('archive_manifest')
    if old_manifest and (Path(snapshot['directory']) / old_manifest['archive']['file']).exists():
        verify_archive(Path(snapshot['directory']), old_manifest)
    result = {'images': remove_images(snapshot['images'], current), 'archive': 'not_recorded'}
    if snapshot.get('archive_manifest'):
        result['archive'] = remove_archive(Path(snapshot['directory']), snapshot['archive_manifest'])
    return result


def clean_server(base, directory):
    value = validate_images(json.loads((directory / 'local-images.json').read_text()))
    if (base / 'current').resolve() != directory.resolve():
        raise ValueError('cleanup_current_release_changed')
    verify_release(str(base), value['sha'], value)
    inspect_images(value)
    report_path = directory / 'image-cleanup.json'
    report = {'sha': value['sha'], 'runtime': 'passed', 'status': 'cleanup_failed'}
    save(report_path, report)
    previous = json.loads((directory / 'previous-images.json').read_text())
    report['previous'] = clean_previous(previous, value)
    report['current_archive'] = remove_archive(directory, value)
    report['status'] = 'cleanup_passed'
    save(report_path, report)
    print(json.dumps(report, sort_keys=True))


def local_previous(pointer, parent):
    name = pointer['directory']
    if Path(name).name != name or name in {'.', '..'}:
        raise ValueError('invalid_previous_evidence_directory')
    directory = parent / name
    value = validate_images(json.loads((directory / 'prepared-release.json').read_text()))
    return {'directory': str(directory), 'archive_manifest': value, 'images': {
        name: {'reference': ref, 'id': value['image_ids'][name]}
        for name, ref in value['images'].items()}}


def clean_local(directory, value, receipt):
    target = receipt['host'] + '\n' + receipt['base_dir']
    key = hashlib.sha256(target.encode()).hexdigest()
    pointer_path = directory.parent / ('.last-deployed-' + key + '.json')
    save(directory / 'image-cleanup.json', {'status': 'cleanup_failed'})
    with pointer_path.with_suffix('.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = update_local(pointer_path, directory, (value, receipt))
    save(directory / 'image-cleanup.json', result)
    return result


def update_local(pointer_path, directory, context):
    value, receipt = context
    report = {'status': 'cleanup_passed', 'previous': 'none'}
    if pointer_path.exists():
        previous = json.loads(pointer_path.read_text())
        if previous['started_at'] >= receipt['started_at']:
            return {'status': 'newer_or_same_deployment_preserved'}
        if previous['directory'] != directory.name:
            report['previous'] = clean_previous(local_previous(previous, directory.parent), value)
    save(pointer_path, {'directory': directory.name, 'started_at': receipt['started_at'],
                        'completed_at': datetime.now(timezone.utc).isoformat()})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['capture', 'clean'])
    parser.add_argument('--base-dir', type=Path, required=True)
    parser.add_argument('--release-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.operation == 'capture':
        capture_previous(args.base_dir, args.release_dir)
    else:
        clean_server(args.base_dir, args.release_dir)


if __name__ == '__main__':
    main()
