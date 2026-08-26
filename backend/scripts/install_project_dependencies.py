from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path


def project_dependencies(
    pyproject_path: Path,
    extra: str | None = None,
) -> tuple[str, ...]:
    with pyproject_path.open("rb") as handle:
        document = tomllib.load(handle)
    project = document.get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml must contain a [project] table")
    build_system = document.get("build-system")
    if not isinstance(build_system, dict):
        raise ValueError("pyproject.toml must contain a [build-system] table")
    build_requires = _string_list(build_system.get("requires"), "build-system.requires")
    project_requires = _string_list(project.get("dependencies"), "project.dependencies")
    dependencies = build_requires + project_requires
    if extra is None:
        return dependencies
    optional = project.get("optional-dependencies")
    if not isinstance(optional, dict) or extra not in optional:
        raise ValueError(f"optional dependency group {extra!r} is not defined")
    return dependencies + _string_list(optional[extra], f"project.optional-dependencies.{extra}")


def _string_list(
    value: object,
    field: str,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return tuple(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install only third-party project dependencies")
    parser.add_argument("pyproject", type=Path)
    parser.add_argument("--extra")
    return parser


def main() -> None:
    args = _parser().parse_args()
    requirements = project_dependencies(args.pyproject, args.extra)
    subprocess.run([sys.executable, "-m", "pip", "install", *requirements], check=True)


if __name__ == "__main__":
    main()
