from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    path = PROJECT_ROOT / "backend" / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"test_script_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_live_solution_script_defaults_to_preview(monkeypatch) -> None:
    module = _load_script("test_solutions_live.py")
    args = module.parse_args([])
    monkeypatch.setattr(module, "SessionLocal", lambda: pytest.fail("preview touched database"))

    assert module.run(args) == 0


def test_live_solution_apply_requires_exact_account_and_target() -> None:
    module = _load_script("test_solutions_live.py")

    with pytest.raises(ValueError, match="account-id"):
        module.validate_apply_args(module.parse_args(["--apply", "--target", "@group"]))
    with pytest.raises(ValueError, match="target"):
        module.validate_apply_args(module.parse_args(["--apply", "--account-id", "1"]))


@pytest.mark.parametrize(
    "script_name",
    [
        "inspect_rescue_details_live.py",
        "inspect_rescue_live.py",
        "probe_all_problematic_groups_live.py",
        "probe_join_request_admin.py",
    ],
)
def test_read_only_live_scripts_are_import_safe(monkeypatch, script_name: str) -> None:
    monkeypatch.setattr(
        "app.database.SessionLocal",
        lambda: pytest.fail("script import opened database"),
    )

    _load_script(script_name)
