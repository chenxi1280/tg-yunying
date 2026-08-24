from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.no_postgres
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GUARDED_SCRIPTS = (
    "authorization-online-abc-manual-outcome.sh",
    "authorization-online-abc-release-interrupted.sh",
    "authorization-online-abc-release-rebind.sh",
    "authorization-online-abc-pending-plan-rebase.sh",
)


@pytest.mark.parametrize("script_name", GUARDED_SCRIPTS)
def test_apply_guard_stops_when_runner_process_exists(tmp_path: Path, script_name: str) -> None:
    docker = tmp_path / "docker"
    marker = tmp_path / "exec-called"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1 $3 $4\" == \"top -eo pid,args\" ]]; then\n"
        "  printf 'PID COMMAND\\n123 python authorization_online_abc_runner.py\\n'\n"
        "  exit 0\n"
        "fi\n"
        "touch \"$FAKE_DOCKER_EXEC_MARKER\"\n",
    )
    docker.chmod(0o755)
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    environment["FAKE_DOCKER_EXEC_MARKER"] = str(marker)

    result = subprocess.run(
        ["bash", str(REPOSITORY_ROOT / "deploy" / script_name), "--mode", "apply"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "runner_present" in result.stderr
    assert not marker.exists()
