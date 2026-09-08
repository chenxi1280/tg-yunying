"""Build and validate immutable production image manifests (no credentials)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

SCHEMA_VERSION = 1
IMAGE_NAMES = (
    "tg-yunying-backend",
    "tg-yunying-frontend",
    "tg-yunying-image-verification-worker",
)
REGISTRY = "ghcr.io/chenxi1280"
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")


def identity(environ: dict) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": environ["GITHUB_REPOSITORY"],
        "sha": environ["GITHUB_SHA"],
        "run_id": int(environ["GITHUB_RUN_ID"]),
        "run_attempt": int(environ["GITHUB_RUN_ATTEMPT"]),
    }


def validate_manifest(value: dict, expected: dict) -> dict:
    if not SHA_PATTERN.fullmatch(expected["sha"]):
        raise ValueError("invalid_candidate_sha")
    for key, item in expected.items():
        if value.get(key) != item:
            raise ValueError(f"prepared_release_identity_mismatch:{key}")
    images = value.get("images")
    if not isinstance(images, dict) or set(images) != set(IMAGE_NAMES):
        raise ValueError("prepared_release_image_set_mismatch")
    for name, reference in images.items():
        prefix = f"{REGISTRY}/{name}@"
        if not isinstance(reference, str) or not reference.startswith(prefix):
            raise ValueError(f"prepared_release_image_repository_mismatch:{name}")
        if not DIGEST_PATTERN.fullmatch(reference.removeprefix(prefix)):
            raise ValueError(f"prepared_release_digest_invalid:{name}")
    return value


def image_manifest(environ: dict) -> dict:
    name, digest = environ["IMAGE_NAME"], environ["IMAGE_DIGEST"]
    if name not in IMAGE_NAMES or not DIGEST_PATTERN.fullmatch(digest):
        raise ValueError("invalid_built_image")
    return {**identity(environ), "images": {name: f"{REGISTRY}/{name}@{digest}"}}


def assemble(directory: Path, expected: dict) -> dict:
    images = {}
    files = sorted(directory.glob("*.json"))
    if len(files) != len(IMAGE_NAMES):
        raise ValueError("prepared_release_image_manifest_count_mismatch")
    for path in files:
        value = json.loads(path.read_text())
        for key, item in expected.items():
            if value.get(key) != item:
                raise ValueError(f"built_image_identity_mismatch:{path.name}:{key}")
        entries = value.get("images", {})
        if len(entries) != 1 or set(entries) & set(images):
            raise ValueError("duplicate_or_invalid_image_manifest")
        images.update(entries)
    return validate_manifest({**expected, "images": images}, expected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("image", "assemble"))
    parser.add_argument("--directory", type=Path, default=Path("image-manifests"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    environ = dict(os.environ)
    value = image_manifest(environ) if args.operation == "image" else assemble(
        args.directory, identity(environ),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
