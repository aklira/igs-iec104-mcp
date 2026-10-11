"""The package skeleton: its version and its modules of IMPLEMENTATION.md §12."""

import importlib
import tomllib
from pathlib import Path

import pytest

import igs_iec104_mcp

ROOT = Path(__file__).resolve().parents[2]


def test_version_matches_pyproject() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert igs_iec104_mcp.__version__ == project["project"]["version"]


@pytest.mark.parametrize(
    "name",
    [
        "server",
        "models",
        "definitions",
        "docker",
        "runtime",
        "scenarios",
        "storage",
        "reporting",
    ],
)
def test_skeleton_module_imports(name: str) -> None:
    assert importlib.import_module(f"igs_iec104_mcp.{name}") is not None
