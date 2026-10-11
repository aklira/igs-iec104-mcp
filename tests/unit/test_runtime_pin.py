"""Decisions D-004 and D-005: the upstream revision and the Rust toolchain are pinned.

The manifests are the source of truth; DECISIONS.md must name the same values.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def decision(identifier: str) -> str:
    """The text of one decision of DECISIONS.md, from its heading to the next one."""
    text = (ROOT / "DECISIONS.md").read_text(encoding="utf-8")
    section = text.split(f"## {identifier}:", 1)[1]
    return section.split("\n## ", 1)[0]


def test_upstream_revision_matches_the_decision() -> None:
    manifest = tomllib.loads(
        (ROOT / "runtime/igs-iec104-runtime/Cargo.toml").read_text(encoding="utf-8")
    )
    dependency = manifest["dependencies"]["igs-iec104"]
    revision = dependency["rev"]
    assert re.fullmatch(r"[0-9a-f]{40}", revision), "the pin must be a full commit hash"
    assert dependency["git"] == "https://github.com/aklira/igs-iec104"
    assert revision in decision("D-004")


def test_toolchain_matches_the_decision() -> None:
    toolchain = tomllib.loads(
        (ROOT / "runtime/rust-toolchain.toml").read_text(encoding="utf-8")
    )
    channel = toolchain["toolchain"]["channel"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", channel), (
        "the toolchain must be a fixed version"
    )
    assert channel in decision("D-005")
