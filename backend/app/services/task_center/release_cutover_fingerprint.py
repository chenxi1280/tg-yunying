"""Inputs whose changes require re-running historical fulfillment takeover."""
from __future__ import annotations

import hashlib
from pathlib import Path

# Changes to runtime writers that alter persisted compatibility must also update
# the corresponding contract version; ordinary behavior changes need no takeover.
CONTRACT_INPUTS = (
    "pyproject.toml", "alembic.ini", "migrations/**/*.py", "app/models/**/*.py",
    "app/database.py", "app/services/task_center/fulfillment*.py",
    "app/services/task_center/ai_content_scope_takeover*.py",
    "app/services/task_center/group_ai_scope*.py",
    "app/services/task_center/payloads.py",
    "app/services/task_center/dispatch_runtime_contract.py",
    "app/services/task_center/release_cutover*.py",
    "scripts/takeover_all_task_fulfillment.py", "scripts/takeover_ai_content_scope.py",
    "scripts/release_worker_cutover.py",
)


def contract_source_fingerprint(backend_root: Path) -> str:
    paths: set[Path] = set()
    for pattern in CONTRACT_INPUTS:
        matches = {path for path in backend_root.glob(pattern) if path.is_file()}
        if not matches:
            raise ValueError(f"release_contract_input_missing:{pattern}")
        paths.update(matches)
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(backend_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()
