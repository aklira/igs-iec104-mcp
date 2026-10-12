"""The reports of an ended run: JSON, JUnit XML, and a readable summary
(IMPLEMENTATION.md §10; task R2).

One document is built from the run's record, its journal, its snapshot, its evidence,
and its artifacts. The JUnit XML and the summary are rendered from that document, so the
three formats agree. Each report is stored once, as an artifact of its run, so it
survives the removal of the test bench. A step's outcome is sorted into a category: an
assertion failure, an infrastructure error, a timeout, an unsupported step, a
cancellation, or an indeterminate outcome. Those are the distinctions a reader needs.
"""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from igs_iec104_mcp.definitions.service import ServiceError
from igs_iec104_mcp.models.run import TERMINAL, Outcome
from igs_iec104_mcp.storage.runs import (
    EVIDENCE_DIRECTORY,
    MAX_PAGE,
    RUNS_DIRECTORY,
    ArtifactRefused,
    RunStore,
)

ARTIFACTS: dict[str, str] = {
    "json": "report.json",
    "junit": "report.junit.xml",
    "summary": "summary.md",
}
REPORT_NAMES: frozenset[str] = frozenset(ARTIFACTS.values())
SCHEMA_VERSION = 1
CATEGORY: dict[Outcome, str] = {
    Outcome.PASSED: "passed",
    Outcome.FAILED: "assertion_failure",
    Outcome.ERROR: "infrastructure_error",
    Outcome.TIMED_OUT: "timeout",
    Outcome.UNSUPPORTED: "unsupported",
    Outcome.CANCELLED: "cancelled",
    Outcome.INDETERMINATE: "indeterminate",
}
# The journal kinds that are notes on the run, rather than verdicts or confirmations.
NOTE_KINDS: frozenset[str] = frozenset(
    {"error", "interrupted", "cancel_requested", "gap"}
)


class ReportOut(BaseModel):
    """A report as a tool returns it: its format, its artifact and the artifact's
    digest, and its text."""

    run: str
    format: str
    artifact: str
    sha256: str
    size: int
    content: str


@dataclass(frozen=True)
class FileEntry:
    """A file a report lists: its name, its SHA-256, and its size."""

    name: str
    sha256: str
    size: int


def report_of(runs: RunStore, run_id: str, fmt: str) -> ReportOut:
    """The report of an ended run in one format. The report is made the first time it is
    asked for, stored as an artifact of the run, and read back afterwards.

    Raises ServiceError: `invalid_argument` for an unknown format, and `invalid_state`
    for a run that has not ended, since its report would change as it runs.
    """
    if fmt not in ARTIFACTS:
        raise ServiceError(
            "invalid_argument",
            f"the report format {fmt!r} is not one of {', '.join(ARTIFACTS)}",
        )
    record = runs.get(run_id)
    if record.status not in TERMINAL:
        raise ServiceError(
            "invalid_state",
            f"the run is {record.status}: its report is made when it ends",
        )
    name = ARTIFACTS[fmt]
    if not any(artifact.name == name for artifact in runs.artifacts(run_id)):
        data = render(build(runs, run_id), fmt).encode("utf-8")
        try:
            runs.add_artifact(run_id, name, data)
        except ArtifactRefused:
            # Another request made it first: the stored report is the one to return.
            if not any(artifact.name == name for artifact in runs.artifacts(run_id)):
                raise
    data = runs.read_artifact(run_id, name)
    return ReportOut(
        run=run_id,
        format=fmt,
        artifact=name,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        content=data.decode("utf-8"),
    )


def build(runs: RunStore, run_id: str) -> dict[str, Any]:
    """The document a report is made of, from what the run kept."""
    record = runs.get(run_id)
    journal = _journal(runs, run_id)
    snapshot = _snapshot(runs, run_id)
    evidence_dir = runs.root / RUNS_DIRECTORY / run_id / EVIDENCE_DIRECTORY
    scenario = snapshot["scenario"]

    verdicts = [event for event in journal if event["kind"] == "verdict"]
    steps: list[dict[str, Any]] = []
    cleanup: list[dict[str, Any]] = []
    for event in verdicts:
        label, _, action = event["step"].partition(": ")
        entry = {
            "label": label,
            "action": action,
            "outcome": event["outcome"],
            "category": CATEGORY[Outcome(event["outcome"])],
            "detail": event["detail"],
            "seconds": event.get("seconds", 0.0),
            "event": event["seq"],
            "expected": _definition(label, scenario),
        }
        (cleanup if label.startswith("cleanup ") else steps).append(entry)

    ended = next((event for event in journal if event["kind"] == "ended"), None)
    engine = next((event for event in journal if event["kind"] == "engine"), None)
    stop_failure = next(
        (
            event["detail"]
            for event in journal
            if event["kind"] == "error" and "did not stop" in event["detail"]
        ),
        None,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "id": record.id,
            "bench": record.bench,
            "scenario": record.scenario,
            "status": str(record.status),
            "reason": ended["reason"] if ended else None,
            "started_at_ms": record.created_at_ms,
            "ended_at_ms": record.ended_at_ms,
        },
        "inputs": {
            "bench_sha256": snapshot["bench_sha256"],
            "scenario_sha256": snapshot["scenario_sha256"],
        },
        "engine": {k: v for k, v in engine.items() if k not in {"kind", "seq"}}
        if engine
        else None,
        "summary": _counts(steps + cleanup),
        "steps": steps,
        "cleanup": {
            "steps": cleanup,
            "bench_stopped": any(event["kind"] == "stopped" for event in journal),
            "stop_failure": stop_failure,
            "bench": _stop_record(evidence_dir),
        },
        "confirmations": [_fields(e) for e in journal if e["kind"] == "confirmation"],
        "notes": [
            {"event": e["seq"], "kind": e["kind"], "detail": e.get("detail", "")}
            for e in journal
            if e["kind"] in NOTE_KINDS
        ],
        "journal": {"events": record.events, "dropped": record.dropped},
        "manifest": {
            "evidence": [_entry(path, evidence_dir) for path in _files(evidence_dir)],
            "artifacts": [
                {"name": a.name, "sha256": a.sha256, "size": a.size}
                for a in runs.artifacts(run_id)
                if a.name not in REPORT_NAMES
            ],
        },
    }


def render(document: dict[str, Any], fmt: str) -> str:
    """The text of a document in one format."""
    if fmt == "json":
        return json.dumps(document, indent=2, sort_keys=True) + "\n"
    if fmt == "junit":
        return _junit(document)
    return _summary(document)


def _journal(runs: RunStore, run_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    after = 0
    while True:
        page = runs.events(run_id, after=after, limit=MAX_PAGE)
        events.extend(page.events)
        if page.next_cursor is None:
            return events
        after = page.next_cursor


def _snapshot(runs: RunStore, run_id: str) -> dict[str, Any]:
    path = runs.root / RUNS_DIRECTORY / run_id / "snapshot.json"
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _stop_record(evidence: Path) -> dict[str, Any] | None:
    path = evidence / "stop.json"
    if not path.is_file():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _fields(event: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key != "kind"}


def _counts(verdicts: list[dict[str, Any]]) -> dict[str, int]:
    counts = {category: 0 for category in CATEGORY.values()}
    for verdict in verdicts:
        counts[verdict["category"]] += 1
    return {"verdicts": len(verdicts), **counts}


def _definition(label: str, scenario: dict[str, Any]) -> Any:
    """The definition a label names in the snapshot of the scenario. `step 2.1.3` is the
    third step of the first branch of step 2; `cleanup 1` is the first cleanup step."""
    where, _, path = label.partition(" ")
    try:
        numbers = [int(part) for part in path.split(".")]
    except ValueError:
        return None
    if where == "cleanup":
        return _at(scenario.get("cleanup", []), numbers) if len(numbers) == 1 else None
    if where != "step" or not numbers:
        return None
    step: Any = _at(scenario.get("steps", []), numbers[:1])
    rest = numbers[1:]
    while step is not None and rest:
        if step.get("action") != "parallel" or len(rest) < 2:
            return None
        branch = _at(step.get("branches", []), rest[:1])
        step = _at(branch, rest[1:2]) if branch is not None else None
        rest = rest[2:]
    return step


def _at(items: list[Any], numbers: list[int]) -> Any:
    """The item at the one-based position `numbers[0]`, or None when there is none."""
    if not numbers or not 1 <= numbers[0] <= len(items):
        return None
    return items[numbers[0] - 1]


def _files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.is_file())


def _entry(path: Path, directory: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "name": str(path.relative_to(directory)),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def _junit(document: dict[str, Any]) -> str:
    run = document["run"]
    cases = document["steps"] + document["cleanup"]["steps"]
    counts = document["summary"]
    suite = ET.Element(
        "testsuite",
        name=run["scenario"],
        tests=str(len(cases)),
        failures=str(counts["assertion_failure"]),
        errors=str(
            counts["infrastructure_error"] + counts["timeout"] + counts["indeterminate"]
        ),
        skipped=str(counts["unsupported"] + counts["cancelled"]),
        time=f"{sum(case['seconds'] for case in cases):.3f}",
    )
    for case in cases:
        element = ET.SubElement(
            suite,
            "testcase",
            classname=run["bench"],
            name=f"{case['label']}: {case['action']}",
            time=f"{case['seconds']:.3f}",
        )
        category = case["category"]
        if category == "assertion_failure":
            ET.SubElement(element, "failure", message=case["detail"], type=category)
        elif category in {"infrastructure_error", "timeout", "indeterminate"}:
            ET.SubElement(element, "error", message=case["detail"], type=category)
        elif category in {"unsupported", "cancelled"}:
            ET.SubElement(element, "skipped", message=case["detail"], type=category)
    root = ET.Element("testsuites")
    root.append(suite)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(
        root, encoding="unicode"
    )


def _summary(document: dict[str, Any]) -> str:
    run = document["run"]
    lines = [
        f"# Run {run['id']}",
        "",
        f"- Scenario: {run['scenario']} on bench {run['bench']}",
        f"- Status: {run['status']}" + (f" ({run['reason']})" if run["reason"] else ""),
        f"- Bench definition SHA-256: {document['inputs']['bench_sha256']}",
        f"- Scenario definition SHA-256: {document['inputs']['scenario_sha256']}",
    ]
    engine = document["engine"]
    if engine:
        lines += [
            f"- Runtime image: {engine['runtime']}",
            f"- Relay image: {engine['relay']}",
            f"- Upstream revision: {engine['upstream']}",
        ]
    counts = document["summary"]
    lines += ["", "## Verdicts", ""]
    lines += [f"- {name.replace('_', ' ')}: {value}" for name, value in counts.items()]
    lines += ["", "## Steps", "", "| Label | Action | Outcome | Seconds | Detail |"]
    lines += ["| --- | --- | --- | --- | --- |"]
    for step in document["steps"] + document["cleanup"]["steps"]:
        lines.append(
            f"| {step['label']} | {step['action']} | {step['category']} | "
            f"{step['seconds']:.3f} | {_cell(step['detail'])} |"
        )
    failed = [s for s in document["steps"] if s["outcome"] != "passed"]
    lines += ["", "## Failures and gaps", ""]
    lines += [f"- {s['label']} ({s['category']}): {s['detail']}" for s in failed] or [
        "None."
    ]
    cleanup = document["cleanup"]
    lines += ["", "## Cleanup", ""]
    lines.append(f"- Bench stopped: {'yes' if cleanup['bench_stopped'] else 'no'}")
    if cleanup["stop_failure"]:
        lines.append(f"- Stop failure: {cleanup['stop_failure']}")
    if cleanup["bench"]:
        lines.append(f"- Graceful: {cleanup['bench'].get('graceful')}")
    lines += ["", "## Confirmations", ""]
    lines += [
        f"- {c['target']}: {c['operation']} "
        f"{'accepted' if c['accepted'] else 'refused'} ({c['how']})"
        for c in document["confirmations"]
    ] or ["None."]
    lines += ["", "## Manifest", ""]
    for entry in document["manifest"]["evidence"] + document["manifest"]["artifacts"]:
        lines.append(f"- {entry['name']}: {entry['sha256']} ({entry['size']} bytes)")
    return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")
