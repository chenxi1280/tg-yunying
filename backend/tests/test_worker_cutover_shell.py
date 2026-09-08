from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.no_postgres
DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
IDS = ("a" * 64, "b" * 64, "c" * 64)


def _environment(tmp_path, *, stop_failure=False, still_running=False):
    inventory = [
        {"Service": "worker-dispatcher-1", "ID": IDS[0]},
        {"Service": "worker-account-login", "ID": IDS[1]},
        {"Service": "image-verification-worker", "ID": IDS[2]},
        {"Service": "backend", "ID": "d" * 64},
        {"Service": "redis", "ID": "e" * 64},
    ]
    (tmp_path / "inventory").write_text("\n".join(json.dumps(row) for row in inventory))
    docker = tmp_path / "docker"
    docker.write_text('''#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$CALLS"
if [[ "$1" == stop ]]; then exit "$STOP_EXIT"; fi
shift 3
for id in "$@"; do printf '%s %s\\n' "$id" "$CONTAINER_STATE"; done
''')
    docker.chmod(0o755)
    return {
        **os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "INVENTORY": str(tmp_path / "inventory"), "CALLS": str(tmp_path / "calls"),
        "STOP_EXIT": "42" if stop_failure else "0",
        "CONTAINER_STATE": "running" if still_running else "exited",
        "SCRIPT_DIR": str(DEPLOY),
    }


def _run(environment):
    script = '''set -euo pipefail
source "$SCRIPT_DIR/worker-cutover.sh"
compose() { cat "$INVENTORY"; }
stop_all_release_workers
echo allowed_to_start_new_workers
'''
    return subprocess.run(["bash", "-c", script], env=environment, capture_output=True, text=True, timeout=5)


def test_stop_freezes_actual_workers_including_disabled_role_in_one_call(tmp_path):
    result = _run(_environment(tmp_path))
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls[0] == "stop " + " ".join(IDS)
    assert len(calls) == 2
    assert "WORKER_STOP_COUNT=3" in result.stdout
    assert "allowed_to_start_new_workers" in result.stdout


@pytest.mark.parametrize("failure", ("stop_failure", "still_running"))
def test_failed_or_incomplete_stop_prevents_starting_new_workers(tmp_path, failure):
    result = _run(_environment(tmp_path, **{failure: True}))
    assert result.returncode == (42 if failure == "stop_failure" else 1)
    if failure == "still_running":
        assert "Release worker remains active" in result.stderr
    assert "allowed_to_start_new_workers" not in result.stdout


def test_bad_inventory_fails_before_any_stop(tmp_path):
    environment = _environment(tmp_path)
    Path(environment["INVENTORY"]).write_text('{"Service":"worker-planner","ID":"--all"}')
    result = _run(environment)
    assert result.returncode != 0
    assert not Path(environment["CALLS"]).exists()
