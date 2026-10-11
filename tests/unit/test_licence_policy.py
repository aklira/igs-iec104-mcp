"""Decision D-007: the licence check admits the policy licences, refuses the others."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_licences", ROOT / "scripts" / "check_licences.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = load_checker()


@pytest.mark.parametrize(
    "expression",
    [
        "MIT",
        "Apache-2.0",
        "ISC",
        "BSD",
        "BSD-3-Clause",
        "MPL-2.0",
        "PSF-2.0",
        "MIT-0",
        "Apache-2.0 OR BSD-3-Clause",
        "GPL-3.0-only OR MIT",
    ],
)
def test_admitted_licences_pass(expression: str) -> None:
    assert checker.is_allowed(expression)


@pytest.mark.parametrize(
    "expression",
    [
        "GPL-3.0-only",
        "LGPL-2.1-only",
        "AGPL-3.0-only",
        "MIT AND GPL-2.0-only",
        "UNKNOWN",
    ],
)
def test_other_licences_are_refused(expression: str) -> None:
    assert not checker.is_allowed(expression)
