from pathlib import Path

import pytest

from scripts.install_project_dependencies import project_dependencies


pytestmark = pytest.mark.no_postgres


def test_project_dependencies_include_requested_optional_group(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
dependencies = ["fastapi>=1", "redis>=5"]

[project.optional-dependencies]
worker = ["rapidocr==3"]

[build-system]
requires = ["setuptools>=69"]
""".strip()
    )

    assert project_dependencies(pyproject) == ("setuptools>=69", "fastapi>=1", "redis>=5")
    assert project_dependencies(pyproject, "worker") == (
        "setuptools>=69",
        "fastapi>=1",
        "redis>=5",
        "rapidocr==3",
    )


def test_project_dependencies_reject_unknown_optional_group(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\ndependencies = []\n[build-system]\nrequires = ['setuptools>=69']\n"
    )

    with pytest.raises(ValueError, match="optional dependency group 'missing' is not defined"):
        project_dependencies(pyproject, "missing")
