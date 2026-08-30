from __future__ import annotations

import fcntl
import os
import pwd
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_FILES = (
    "antigravity_provider_server.py",
    "antigravity_provider_ledger.py",
    "antigravity_provider_protocol.py",
    "antigravity_provider_schema.py",
)


def _executable(path: Path, source: str) -> Path:
    path.write_text(source)
    path.chmod(0o755)
    return path


def _fake_systemctl(fake_bin: Path) -> Path:
    return _executable(fake_bin / "systemctl", """#!/usr/bin/env bash
set -eu
command_name="$1"
unit_name="${@: -1}"
case "${command_name}" in
  is-enabled) [[ "${FAKE_SLOT_ENABLED:-0}" == 1 ]] ;;
  is-active) [[ -f "${FAKE_SLOT_STATE_DIR}/${unit_name}" ]] ;;
  is-failed) exit 1 ;;
  restart) touch "${FAKE_SLOT_STATE_DIR}/${unit_name}"; echo "restart:${unit_name}" >>"${FAKE_SLOT_LOG}" ;;
  stop) rm -f "${FAKE_SLOT_STATE_DIR}/${unit_name}"; echo "stop:${unit_name}" >>"${FAKE_SLOT_LOG}" ;;
  *) exit 2 ;;
esac
""")


def _slot_env(
    tmp_path: Path, *, enabled: bool, active: bool, slots: int = 1,
) -> tuple[dict, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True)
    _fake_systemctl(fake_bin)
    _executable(fake_bin / "flock", """#!/usr/bin/env python3
import fcntl
import sys
try:
    fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(1)
""")
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    state_dir = tmp_path / "states"
    state_dir.mkdir()
    for number in range(1, slots + 1):
        unit_name = f"tgyunying-antigravity-slot-{number:02d}.service"
        (unit_dir / unit_name).write_text("unit")
        if active:
            (state_dir / unit_name).write_text("active")
    log = tmp_path / "slot.log"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ANTIGRAVITY_SLOT_UNIT_DIR": str(unit_dir),
        "FAKE_SLOT_ENABLED": str(int(enabled)),
        "FAKE_SLOT_STATE_DIR": str(state_dir),
        "FAKE_SLOT_LOG": str(log),
    }
    return env, log


def _runtime_env(tmp_path: Path) -> tuple[dict, Path, Path]:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for name in RUNTIME_FILES:
        (source_dir / name).write_text(f"source:{name}\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(fake_bin / "install", """#!/usr/bin/env bash
set -eu
directory=0
mode=""
values=()
while (($#)); do
  case "$1" in
    -d) directory=1; shift ;;
    -o|-g) shift 2 ;;
    -m) mode="$2"; shift 2 ;;
    *) values+=("$1"); shift ;;
  esac
done
if (( directory )); then mkdir -p "${values[@]}"; chmod "${mode}" "${values[@]}"; exit; fi
cp "${values[0]}" "${values[1]}"
chmod "${mode}" "${values[1]}"
""")
    _executable(fake_bin / "stat", """#!/usr/bin/env bash
set -eu
format="$2"
path="$3"
if [[ "${format}" == "%U:%G" ]]; then echo "${FAKE_STAT_OWNER:-root:root}"; exit; fi
if [[ -d "${path}" ]]; then echo "${FAKE_DIR_MODE:-755}"; else echo "${FAKE_FILE_MODE:-644}"; fi
""")
    runtime_root = tmp_path / "runtime"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "SOURCE_DIR": str(source_dir),
        "RELEASE_SHA": "a" * 40,
        "ANTIGRAVITY_RUNTIME_ROOT": str(runtime_root),
        "ANTIGRAVITY_RUNTIME_PYTHON_BIN": sys.executable,
    }
    return env, source_dir, runtime_root


def test_slot_service_keeps_runtime_and_ledger_outside_data_tree():
    source = (PROJECT_ROOT / "deploy/install-antigravity-provider-slot.sh").read_text()
    assert 'SERVICE_HOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"' in source
    assert "ANTIGRAVITY_LEDGER_PATH=${service_ledger}" in source
    assert "ReadWritePaths=${SERVICE_AUTH_DIR} ${SERVICE_CONFIG_DIR} ${SERVICE_CACHE_DIR} ${SERVICE_DATA_DIR}" in source
    assert "ReadWritePaths=${SHARED_DIR}" not in source
    assert "ProtectSystem=strict" in source
    assert "ProtectHome=read-only" in source
    assert "WorkingDirectory=${RUNTIME_ROOT}/current" in source
    assert "ExecStart=${PYTHON_BIN} -E -s" in source
    assert '"${PYTHON_BIN}" -E -s -c' in source
    assert 'ledger_key="$("${PYTHON_BIN}" -E -s -' in source
    assert '"${PYTHON_BIN}" -E -s "${SCRIPT_DIR}/migrate-' in source
    assert 'PYTHON_BIN="${ANTIGRAVITY_PYTHON_BIN:-/usr/bin/python3.11}"' in source
    assert "${BASE_DIR}/current/backend/scripts/antigravity_provider_server.py" not in source


def test_python_isolation_excludes_service_owned_user_site(tmp_path: Path):
    system_python = "/usr/bin/python3"
    user_base = tmp_path / "user-base"
    env = {**os.environ, "PYTHONUSERBASE": str(user_base)}
    site_path = subprocess.check_output(
        [system_python, "-c", "import site; print(site.getusersitepackages())"],
        env=env, text=True,
    ).strip()
    module_dir = Path(site_path)
    module_dir.mkdir(parents=True)
    (module_dir / "service_owned_probe.py").write_text("VALUE = 'loaded'\n")
    loaded = subprocess.run(
        [system_python, "-c", "import service_owned_probe"], env=env,
        capture_output=True,
    )
    isolated = subprocess.run(
        [system_python, "-E", "-s", "-c", "import service_owned_probe"], env=env,
        capture_output=True,
    )
    assert loaded.returncode == 0
    assert isolated.returncode != 0


def test_legacy_wal_ledger_migration_is_complete_and_blocks_open_states(tmp_path: Path):
    source = tmp_path / "legacy.sqlite3"
    destination = tmp_path / "service.sqlite3"
    helper = PROJECT_ROOT / "deploy/migrate-antigravity-provider-ledger.py"
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE requests (request_id TEXT PRIMARY KEY, state TEXT)")
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("INSERT INTO requests VALUES ('confirmed-request', 'confirmed')")
    connection.commit()
    assert source.with_name(source.name + "-wal").stat().st_size > 0
    owner = pwd.getpwuid(os.getuid()).pw_name
    migrated = subprocess.run(
        [sys.executable, str(helper), "-", str(source), str(destination), owner],
        capture_output=True,
    )
    assert migrated.returncode == 0
    with sqlite3.connect(destination) as copied:
        assert copied.execute("SELECT * FROM requests").fetchall() == [
            ("confirmed-request", "confirmed"),
        ]
    connection.execute("INSERT INTO requests VALUES ('new-confirmed', 'confirmed')")
    connection.commit()
    stale = subprocess.run(
        [sys.executable, str(helper), str(source), str(source), str(destination), owner],
        capture_output=True,
    )
    assert stale.returncode != 0
    assert b"antigravity_ledger_authority_drift" in stale.stderr
    service_authority = subprocess.run(
        [sys.executable, str(helper), str(destination), str(source), str(destination), owner],
        capture_output=True,
    )
    assert service_authority.returncode == 0
    connection.execute("INSERT INTO requests VALUES ('unknown-request', 'unknown')")
    connection.commit()
    blocked_destination = tmp_path / "blocked.sqlite3"
    blocked = subprocess.run(
        [sys.executable, str(helper), "-", str(source), str(blocked_destination), owner],
        capture_output=True,
    )
    connection.close()
    assert blocked.returncode != 0
    assert b"antigravity_legacy_ledger_reconcile_required" in blocked.stderr
    assert not blocked_destination.exists()


def test_empty_cutover_is_durable_and_missing_service_authority_fails(tmp_path: Path):
    helper = PROJECT_ROOT / "deploy/migrate-antigravity-provider-ledger.py"
    legacy = tmp_path / "missing-legacy.sqlite3"
    destination = tmp_path / "missing-service.sqlite3"
    owner = pwd.getpwuid(os.getuid()).pw_name
    result = subprocess.run(
        [sys.executable, str(helper), str(legacy), str(legacy), str(destination), owner],
        capture_output=True,
    )
    assert result.returncode == 0
    assert b"ANTIGRAVITY_LEDGER_ACTION=created_empty" in result.stdout
    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
    crash_retry = subprocess.run(
        [
            sys.executable, str(helper), str(legacy), str(legacy),
            str(destination), owner,
        ],
        capture_output=True,
    )
    assert crash_retry.returncode == 0
    assert b"ANTIGRAVITY_LEDGER_ACTION=verified_staged_empty" in crash_retry.stdout
    repeated = subprocess.run(
        [
            sys.executable, str(helper), str(destination), str(legacy),
            str(destination), owner,
        ],
        capture_output=True,
    )
    assert repeated.returncode == 0
    destination.unlink()
    missing = subprocess.run(
        [
            sys.executable, str(helper), str(destination), str(legacy),
            str(destination), owner,
        ],
        capture_output=True,
    )
    assert missing.returncode != 0
    assert b"antigravity_service_ledger_missing" in missing.stderr


def test_staged_empty_ledger_requires_canonical_schema(tmp_path: Path):
    helper = PROJECT_ROOT / "deploy/migrate-antigravity-provider-ledger.py"
    legacy = tmp_path / "missing-legacy.sqlite3"
    destination = tmp_path / "invalid-service.sqlite3"
    with sqlite3.connect(destination) as connection:
        connection.execute("CREATE TABLE requests (request_id TEXT PRIMARY KEY, state TEXT)")
    owner = pwd.getpwuid(os.getuid()).pw_name
    result = subprocess.run(
        [
            sys.executable, str(helper), str(legacy), str(legacy),
            str(destination), owner,
        ],
        capture_output=True,
    )
    assert result.returncode != 0
    assert b"antigravity_staged_empty_schema_invalid" in result.stderr


@pytest.mark.parametrize(
    ("load_state", "active_state", "main_pid", "allowed"),
    [
        ("not-found", "inactive", "0", True),
        ("loaded", "inactive", "0", True),
        ("loaded", "activating", "123", False),
        ("loaded", "deactivating", "123", False),
        ("loaded", "failed", "0", False),
        ("not-found", "inactive", "123", False),
    ],
)
def test_slot_install_state_requires_exact_inactive_without_pid(
    tmp_path: Path, load_state: str, active_state: str, main_pid: str, allowed: bool,
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(fake_bin / "systemctl", """#!/usr/bin/env bash
printf 'LoadState=%s\nActiveState=%s\nMainPID=%s\n' \
  "${FAKE_LOAD_STATE}" "${FAKE_ACTIVE_STATE}" "${FAKE_MAIN_PID}"
""")
    env = {
        **os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_LOAD_STATE": load_state, "FAKE_ACTIVE_STATE": active_state,
        "FAKE_MAIN_PID": main_pid,
    }
    script = PROJECT_ROOT / "deploy/check-antigravity-slot-install-state.sh"
    result = subprocess.run(["bash", str(script), "slot.service"], env=env, capture_output=True)
    assert (result.returncode == 0) is allowed


def test_runtime_installer_is_atomic_idempotent_and_detects_content_drift(tmp_path: Path):
    env, source_dir, runtime_root = _runtime_env(tmp_path)
    script = PROJECT_ROOT / "deploy/install-antigravity-provider-runtime.sh"
    subprocess.run(["bash", str(script)], env=env, check=True, capture_output=True)
    target = runtime_root / "releases" / ("a" * 40)
    assert (runtime_root / "current").resolve() == target
    assert all((target / name).read_text() == (source_dir / name).read_text() for name in RUNTIME_FILES)
    subprocess.run(["bash", str(script)], env=env, check=True, capture_output=True)
    (target / RUNTIME_FILES[0]).write_text("drift")
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True)
    assert result.returncode != 0
    assert b"content drift" in result.stderr
    (target / RUNTIME_FILES[0]).write_text((source_dir / RUNTIME_FILES[0]).read_text())
    (runtime_root / "current").unlink()
    (runtime_root / "current").mkdir()
    invalid = subprocess.run(["bash", str(script)], env=env, capture_output=True)
    assert invalid.returncode != 0
    assert b"ANTIGRAVITY_RUNTIME_CURRENT_INVALID" in invalid.stderr


def test_runtime_installer_rejects_owner_and_mode_drift(tmp_path: Path):
    env, _source_dir, _runtime_root = _runtime_env(tmp_path)
    script = PROJECT_ROOT / "deploy/install-antigravity-provider-runtime.sh"
    subprocess.run(["bash", str(script)], env=env, check=True, capture_output=True)
    owner_result = subprocess.run(
        ["bash", str(script)], env={**env, "FAKE_STAT_OWNER": "slot:slot"},
        capture_output=True,
    )
    mode_result = subprocess.run(
        ["bash", str(script)], env={**env, "FAKE_FILE_MODE": "664"},
        capture_output=True,
    )
    assert owner_result.returncode != 0
    assert mode_result.returncode != 0


def test_slot_release_plan_state_matrix(tmp_path: Path):
    script = PROJECT_ROOT / "deploy/antigravity-slot-release-plan.sh"
    disabled_env, _log = _slot_env(tmp_path / "disabled", enabled=False, active=False)
    disabled = subprocess.run(["bash", str(script)], env=disabled_env, capture_output=True)
    assert disabled.returncode == 0 and disabled.stdout == b""
    assert b"disabled_inactive" in disabled.stderr
    drift_env, _log = _slot_env(tmp_path / "drift", enabled=False, active=True)
    drift = subprocess.run(["bash", str(script)], env=drift_env, capture_output=True)
    assert drift.returncode != 0 and b"disabled_active" in drift.stderr
    enabled_env, _log = _slot_env(tmp_path / "enabled", enabled=True, active=False)
    enabled = subprocess.run(["bash", str(script)], env=enabled_env, capture_output=True)
    assert enabled.returncode == 0
    assert b"tgyunying-antigravity-slot-01.service" in enabled.stdout


def test_restart_script_skips_disabled_and_probes_enabled_slot(tmp_path: Path):
    script = PROJECT_ROOT / "deploy/restart-antigravity-provider-slots.sh"
    disabled_env, disabled_log = _restart_env(tmp_path / "disabled", enabled=False)
    subprocess.run(["bash", str(script)], env=disabled_env, check=True, capture_output=True)
    assert not disabled_log.exists()
    enabled_env, enabled_log = _restart_env(tmp_path / "enabled", enabled=True)
    subprocess.run(["bash", str(script)], env=enabled_env, check=True, capture_output=True)
    log = enabled_log.read_text()
    assert "runtime-installed" in log
    assert "restart:tgyunying-antigravity-slot-01.service" in log
    assert "probe:http://172.18.0.1:18101:" + "a" * 40 in log
    assert Path(enabled_env["ANTIGRAVITY_RUNTIME_ROOT"]).joinpath("current").resolve().name == "new"
    failed_env, _failed_log = _restart_env(tmp_path / "failed", enabled=True)
    failed = subprocess.run(
        ["bash", str(script)], env={**failed_env, "FAKE_PROBE_EXIT": "1"},
        capture_output=True,
    )
    assert failed.returncode != 0
    assert b"ANTIGRAVITY_SLOT_ROLLBACK=complete" in failed.stderr
    failed_current = Path(failed_env["ANTIGRAVITY_RUNTIME_ROOT"]) / "current"
    assert failed_current.resolve().name == "old"


def test_second_slot_probe_failure_restores_runtime_and_all_units(tmp_path: Path):
    script = PROJECT_ROOT / "deploy/restart-antigravity-provider-slots.sh"
    env, log_path = _restart_env(tmp_path, enabled=True, slots=2)
    result = subprocess.run(
        ["bash", str(script)],
        env={**env, "FAKE_PROBE_FAIL_URL": "http://172.18.0.1:18102"},
        capture_output=True,
    )
    assert result.returncode != 0
    assert b"ANTIGRAVITY_SLOT_ROLLBACK=complete units=2" in result.stderr
    assert (Path(env["ANTIGRAVITY_RUNTIME_ROOT"]) / "current").resolve().name == "old"
    log = log_path.read_text()
    assert log.count("restart:tgyunying-antigravity-slot-01.service") == 2
    assert log.count("restart:tgyunying-antigravity-slot-02.service") == 2


def test_probe_failure_restores_initially_inactive_unit_to_stopped(tmp_path: Path):
    script = PROJECT_ROOT / "deploy/restart-antigravity-provider-slots.sh"
    env, log_path = _restart_env(tmp_path, enabled=True, active=False)
    result = subprocess.run(
        ["bash", str(script)], env={**env, "FAKE_PROBE_EXIT": "1"},
        capture_output=True,
    )
    assert result.returncode != 0
    log = log_path.read_text()
    assert log.count("restart:tgyunying-antigravity-slot-01.service") == 1
    assert "stop:tgyunying-antigravity-slot-01.service" in log


def test_restart_refuses_concurrent_slot_operation(tmp_path: Path):
    script = PROJECT_ROOT / "deploy/restart-antigravity-provider-slots.sh"
    env, _log_path = _restart_env(tmp_path, enabled=True)
    lock_path = Path(env["ANTIGRAVITY_SLOT_LOCK_FILE"])
    with lock_path.open("w") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(["bash", str(script)], env=env, capture_output=True)
    assert result.returncode != 0
    assert b"ANTIGRAVITY_SLOT_OPERATION_LOCKED" in result.stderr


def _restart_env(
    tmp_path: Path, *, enabled: bool, slots: int = 1, active: bool | None = None,
) -> tuple[dict, Path]:
    initial_active = enabled if active is None else active
    env, log = _slot_env(
        tmp_path, enabled=enabled, active=initial_active, slots=slots,
    )
    base_dir = tmp_path / "base"
    release_dir = tmp_path / "release"
    for number in range(1, slots + 1):
        slot_dir = base_dir / f"shared/antigravity/slot-{number:02d}"
        slot_dir.mkdir(parents=True)
        (slot_dir / "provider.env").write_text("ANTIGRAVITY_BRIDGE_TOKEN=test-token\n")
    (release_dir / "backend/scripts").mkdir(parents=True)
    (release_dir / "deploy").mkdir()
    (release_dir / ".image.env").write_text("RELEASE_SHA=" + "a" * 40 + "\n")
    fake_bin = Path(env["PATH"].split(":", 1)[0])
    _executable(fake_bin / "docker", """#!/usr/bin/env bash
echo 172.18.0.1
""")
    runtime_root = tmp_path / "runtime"
    (runtime_root / "old").mkdir(parents=True)
    (runtime_root / "new").mkdir()
    (runtime_root / "current").symlink_to(runtime_root / "old", target_is_directory=True)
    runtime = _executable(tmp_path / "runtime-installer.sh", """#!/usr/bin/env bash
echo runtime-installed >>"${FAKE_SLOT_LOG}"
ln -sfn "${FAKE_NEW_RUNTIME}" "${ANTIGRAVITY_RUNTIME_ROOT}/current.tmp"
"${ANTIGRAVITY_RUNTIME_PYTHON_BIN}" -c \
  'import os, sys; os.replace(sys.argv[1], sys.argv[2])' \
  "${ANTIGRAVITY_RUNTIME_ROOT}/current.tmp" "${ANTIGRAVITY_RUNTIME_ROOT}/current"
""")
    timeout = _executable(tmp_path / "timeout.sh", """#!/usr/bin/env bash
echo "probe:${ANTIGRAVITY_BRIDGE_URL}:${RELEASE_SHA}" >>"${FAKE_SLOT_LOG}"
if [[ "${ANTIGRAVITY_BRIDGE_URL}" == "${FAKE_PROBE_FAIL_URL:-}" ]]; then exit 1; fi
exit "${FAKE_PROBE_EXIT:-0}"
""")
    return {
        **env,
        "BASE_DIR": str(base_dir),
        "RELEASE_DIR": str(release_dir),
        "ANTIGRAVITY_RUNTIME_INSTALLER": str(runtime),
        "ANTIGRAVITY_SLOT_LOCK_FILE": str(tmp_path / "operation.lock"),
        "ANTIGRAVITY_RUNTIME_ROOT": str(runtime_root),
        "ANTIGRAVITY_RUNTIME_PYTHON_BIN": sys.executable,
        "FAKE_NEW_RUNTIME": str(runtime_root / "new"),
        "ANTIGRAVITY_TIMEOUT_BIN": str(timeout),
        "ANTIGRAVITY_PYTHON_BIN": "/bin/true",
    }, log


def test_server_preflight_precedes_mutation_and_live_follows_slot_probe():
    source = (PROJECT_ROOT / "deploy/server-install-release.sh").read_text()
    preflight = source.index("antigravity-slot-release-plan.sh")
    prepare = source.index("prepare_shared_layout", source.index("require_command docker"))
    compose = source.index('bash "${RELEASE_DIR}/deploy/compose-up.sh"')
    restart = source.index("restart-antigravity-provider-slots.sh")
    live = source.index('echo "Release ${RELEASE_ID} is live"')
    assert preflight < prepare < compose
    assert compose < restart < live
