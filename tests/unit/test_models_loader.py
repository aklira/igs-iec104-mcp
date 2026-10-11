"""The YAML loader: safe, strict about keys, and it reports each problem by its path."""

from pathlib import Path

import pytest

from igs_iec104_mcp.models.common import DefinitionError
from igs_iec104_mcp.models.loader import load_bench, load_document, load_scenario

ROOT = Path(__file__).resolve().parents[2]


def test_a_python_object_tag_is_refused_and_builds_nothing() -> None:
    with pytest.raises(DefinitionError, match="not valid YAML"):
        load_document("schema_version: !!python/object/apply:os.system ['true']\n")


def test_a_key_given_twice_is_refused() -> None:
    with pytest.raises(DefinitionError, match="not valid YAML"):
        load_document("name: a\nname: b\n")


def test_a_definition_that_is_a_list_is_refused() -> None:
    with pytest.raises(DefinitionError, match="a definition is a mapping"):
        load_bench("- one\n- two\n")


def test_every_problem_of_a_bench_is_reported_with_its_path() -> None:
    text = """
schema_version: 2
name: Bad Name
networks:
  - name: lan
    extra: true
instances:
  - name: server-1
    role: server
    network: lan
    common_address: 0
"""
    with pytest.raises(DefinitionError) as error:
        load_bench(text)
    paths = {issue.path for issue in error.value.issues}
    assert {
        "schema_version",
        "name",
        "networks.0.extra",
        "instances.0.common_address",
    } <= paths
    messages = " ".join(issue.message for issue in error.value.issues)
    assert "common address 0 is out of range" in messages


def test_the_examples_of_the_plan_load_and_check() -> None:
    from igs_iec104_mcp.models.scenario import check_scenario

    bench = load_bench(
        (ROOT / "examples/benches/basic-pair.yaml").read_text(encoding="utf-8")
    )
    scenario = load_scenario(
        (
            ROOT / "examples/scenarios/interrogation-and-spontaneous-update.yaml"
        ).read_text(encoding="utf-8")
    )
    check_scenario(scenario, bench)
