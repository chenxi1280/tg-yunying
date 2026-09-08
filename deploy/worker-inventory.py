"""Select existing worker container IDs from this Compose project's inventory."""
from __future__ import annotations

import json
import re
import sys


def worker_ids(content: str) -> tuple[str, ...]:
    if not content.strip():
        return ()
    rows = json.loads(content) if content.lstrip().startswith("[") else [
        json.loads(line) for line in content.splitlines() if line.strip()
    ]
    result = set()
    for row in rows:
        service = row["Service"]
        if not (service.startswith("worker-") or service == "image-verification-worker"):
            continue
        container_id = row["ID"]
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            raise ValueError("release_worker_container_id_invalid")
        result.add(container_id)
    return tuple(sorted(result))


if __name__ == "__main__":
    for container_id in worker_ids(sys.stdin.read()):
        print(container_id)
