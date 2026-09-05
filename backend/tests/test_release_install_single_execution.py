import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _executable(directory, name, content):
    path = directory / name
    path.write_text(content)
    path.chmod(0o755)


def _repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "release", str(repo)], check=True)
    (repo / "release.txt").write_text("immutable candidate")
    subprocess.run(["git", "add", "release.txt"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-q", "-m", "candidate"], cwd=repo, check=True)
    return repo


def _environment(tmp_path, exit_code):
    commands = tmp_path / "commands"
    commands.mkdir()
    _executable(commands, "ssh", '''#!/usr/bin/env bash
if [[ "${@: -1}" == "true" ]]; then exit 0; fi
echo 'install_started' >> "${INSTALL_CALL_LOG}"
echo 'remote phase failed' >&2
exit "${INSTALL_EXIT_CODE}"
''')
    _executable(commands, "scp", "#!/usr/bin/env bash\nexit 0\n")
    _executable(commands, "timeout", '#!/usr/bin/env bash\nshift\nexec "$@"\n')
    _executable(commands, "sleep", "#!/usr/bin/env bash\nexit 0\n")
    return {**os.environ, "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
        "INSTALL_CALL_LOG": str(tmp_path / "install.log"), "INSTALL_EXIT_CODE": str(exit_code),
        "RELEASE_SSH_ATTEMPTS": "3", "RELEASE_SSH_RETRY_DELAY": "1"}


@pytest.mark.parametrize("exit_code", (1, 255, 124))
def test_remote_install_failure_is_not_replayed(tmp_path, exit_code):
    environment = _environment(tmp_path, exit_code)
    result = subprocess.run(["bash", str(PROJECT_ROOT / "deploy/release.sh"),
        "--host", "test.invalid"], cwd=_repository(tmp_path), env=environment,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)

    assert result.returncode == exit_code
    assert (tmp_path / "install.log").read_text().splitlines() == ["install_started"]
    assert "remote phase failed" in result.stderr
    assert " completed" not in result.stdout
    assert "retrying" not in result.stderr
