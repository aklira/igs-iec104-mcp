"""The reports of an ended run (IMPLEMENTATION.md §10; task R2).

A report is made from what the run kept, once, and stored with the run. Each step's
outcome is sorted into a category, so an assertion failure, an infrastructure error,
and an unsupported step read differently in every format. The JUnit XML is valid, with
one test case per step, and the summary is readable Markdown.
"""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from test_scenario_runner import INTERROGATE, FakeEngine, project, settled

from igs_iec104_mcp.definitions import service
from igs_iec104_mcp.definitions.service import ServiceError
from igs_iec104_mcp.definitions.store import ProjectStore
from igs_iec104_mcp.models.run import RunStatus
from igs_iec104_mcp.reporting.reports import _definition, report_of
from igs_iec104_mcp.runtime.pin import UPSTREAM_REVISION
from igs_iec104_mcp.scenarios.runner import Runner

ROOT = Path(__file__).resolve().parents[2]
WRONG_VALUE = INTERROGATE.replace("name: interrogate", "name: wrong").replace(
    "value: 12.5", "value: 99.0"
)
MANAGED_COMMAND = """\
schema_version: 1
name: managed
bench: basic-pair
steps:
  - action: send_command
    target: client-1
    address: 100
    command: single
    value: true
    timeout: 5s
"""


def add_scenario(tmp_path: Path, name: str, text: str) -> None:
    service.create(ProjectStore(tmp_path), "scenario", name, text)


async def run_to_end(runner: Runner, name: str) -> str:
    started = await runner.start(name)
    await settled(runner, started.id)
    return started.id


def snapshot_hashes(tmp_path: Path, run_id: str) -> dict[str, str]:
    snapshot = json.loads(
        (tmp_path / "runs" / run_id / "snapshot.json").read_text(encoding="utf-8")
    )
    return {
        "bench_sha256": snapshot["bench_sha256"],
        "scenario_sha256": snapshot["scenario_sha256"],
    }


async def test_a_passed_run_is_reported_with_its_expectations_and_inputs(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path)
    engine = FakeEngine()
    runner = Runner(store, runs, engine)
    run_id = await run_to_end(runner, "interrogate")

    report = report_of(runs, run_id, "json")
    document = json.loads(report.content)
    assert document["run"]["status"] == "passed"
    assert document["run"]["reason"] == "every step passed"
    assert document["summary"]["passed"] == 3
    assert [s["category"] for s in document["steps"]] == ["passed"] * 3
    assert document["steps"][0]["expected"]["action"] == "wait_transfer_started"
    assert document["steps"][2]["expected"]["value"] == 12.5
    assert all(s["event"] > 0 and s["seconds"] >= 0 for s in document["steps"])
    assert document["inputs"] == {**snapshot_hashes(tmp_path, run_id), "secrets": []}
    assert document["engine"]["runtime"] == "sha256:fake"
    assert engine.handles[0].stopped, "the report is made after the bench is removed"
    assert document["cleanup"]["bench_stopped"] is True


async def test_each_format_is_stored_once_and_read_back(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, FakeEngine())
    run_id = await run_to_end(runner, "interrogate")
    first = report_of(runs, run_id, "junit")
    again = report_of(runs, run_id, "junit")
    assert first.sha256 == again.sha256
    assert {a.name for a in runs.artifacts(run_id)} == {"report.junit.xml"}
    summary = report_of(runs, run_id, "summary")
    assert summary.artifact == "summary.md"
    json_report = report_of(runs, run_id, "json")
    assert json.loads(json_report.content)["manifest"]["artifacts"] == []


async def test_failures_are_sorted_into_their_categories(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    add_scenario(tmp_path, "wrong", WRONG_VALUE)
    add_scenario(tmp_path, "managed", MANAGED_COMMAND)
    runner = Runner(store, runs, FakeEngine())
    wrong = await run_to_end(runner, "wrong")
    managed = await run_to_end(runner, "managed")

    assert runs.get(wrong).status is RunStatus.FAILED
    failed = json.loads(report_of(runs, wrong, "json").content)
    assert [s["category"] for s in failed["steps"]] == [
        "passed",
        "passed",
        "assertion_failure",
    ]
    assert failed["summary"]["assertion_failure"] == 1

    assert runs.get(managed).status is RunStatus.ERROR
    unsupported = json.loads(report_of(runs, managed, "json").content)
    assert unsupported["steps"][0]["category"] == "unsupported"


async def test_the_junit_report_is_valid_and_has_one_case_per_step(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path)
    add_scenario(tmp_path, "wrong", WRONG_VALUE)
    runner = Runner(store, runs, FakeEngine())
    run_id = await run_to_end(runner, "wrong")
    text = report_of(runs, run_id, "junit").content
    root = ET.fromstring(text.split("\n", 1)[1])
    suite = root.find("testsuite")
    assert suite is not None
    assert suite.get("tests") == "3"
    assert suite.get("failures") == "1"
    assert suite.get("errors") == "0"
    cases = suite.findall("testcase")
    assert cases[0].get("name", "").startswith("step 1: wait_transfer_started")
    failure = cases[2].find("failure")
    assert failure is not None
    assert failure.get("type") == "assertion_failure"
    assert cases[0].find("failure") is None


async def test_an_unsupported_step_is_skipped_in_junit_not_failed(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path)
    add_scenario(tmp_path, "managed", MANAGED_COMMAND)
    runner = Runner(store, runs, FakeEngine())
    run_id = await run_to_end(runner, "managed")
    suite = ET.fromstring(report_of(runs, run_id, "junit").content.split("\n", 1)[1])
    case = suite.find("testsuite/testcase")
    assert case is not None
    skipped = case.find("skipped")
    assert skipped is not None
    assert skipped.get("type") == "unsupported"
    assert case.find("failure") is None and case.find("error") is None


async def test_the_summary_reads_as_markdown_and_lists_the_failures(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path)
    add_scenario(tmp_path, "wrong", WRONG_VALUE)
    runner = Runner(store, runs, FakeEngine())
    run_id = await run_to_end(runner, "wrong")
    text = report_of(runs, run_id, "summary").content
    assert text.startswith(f"# Run {run_id}\n")
    assert "## Failures and gaps" in text
    assert re.search(r"step 3 \(assertion_failure\)", text)
    assert "- Upstream revision: fake" in text, "the engine of this test is a fake"


async def test_a_run_that_has_not_ended_has_no_report(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, FakeEngine(transfer="stopped"))
    started = await runner.start("interrogate")
    with pytest.raises(ServiceError) as error:
        runner.report(started.id, format="json")
    assert error.value.code == "invalid_state"
    await runner.cancel(started.id)
    await settled(runner, started.id)


async def test_an_unknown_format_is_refused(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    runner = Runner(store, runs, FakeEngine())
    run_id = await run_to_end(runner, "interrogate")
    with pytest.raises(ServiceError) as error:
        runner.report(run_id, format="pdf")
    assert error.value.code == "invalid_argument"


def test_the_label_names_its_definition_in_the_snapshot() -> None:
    nested = {"action": "parallel", "branches": [[{"action": "a"}, {"action": "b"}]]}
    scenario: dict[str, Any] = {
        "steps": [
            {"action": "x"},
            {
                "action": "parallel",
                "branches": [[{"action": "c"}], [{"action": "d"}, nested]],
            },
        ],
        "cleanup": [{"action": "clean"}],
    }
    assert _definition("step 1", scenario) == {"action": "x"}
    assert _definition("step 2.2.1", scenario) == {"action": "d"}
    assert _definition("step 2.2.2.1.2", scenario) == {"action": "b"}
    assert _definition("cleanup 1", scenario) == {"action": "clean"}
    assert _definition("step 3", scenario) is None
    assert _definition("step 2.2.5", scenario) is None


def test_the_upstream_revision_is_the_one_the_runtime_builds_against() -> None:
    manifest = (ROOT / "runtime/igs-iec104-runtime/Cargo.toml").read_text(
        encoding="utf-8"
    )
    assert f'rev = "{UPSTREAM_REVISION}"' in manifest
