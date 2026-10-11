"""The confirmation of state-changing operations on external targets (IMPLEMENTATION.md
§9; task M3).

A command to an external target is refused when the target does not allow it, refused
when no person confirms it and no reviewed opt-in covers it, and recorded with its
decision when it is confirmed. A client that supports elicitation is asked; a reviewed
opt-in decides only when the client cannot be asked.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.types import ClientCapabilities, ElicitResult
from test_scenario_runner import BENCH, FakeEngine, kinds, settled

from igs_iec104_mcp.definitions import service
from igs_iec104_mcp.definitions.service import ServiceError
from igs_iec104_mcp.definitions.store import ProjectStore
from igs_iec104_mcp.models.loader import load_bench, load_document
from igs_iec104_mcp.models.run import RunStatus
from igs_iec104_mcp.models.target import ExternalTarget
from igs_iec104_mcp.scenarios.confirmation import ElicitingConfirmer
from igs_iec104_mcp.scenarios.runner import Runner
from igs_iec104_mcp.storage.runs import RunStore

EXTERNAL = """external_targets:
  - name: ext
    role: client
    network: wan
    endpoint:
      host: 127.0.0.1
      port: 2404
    common_address: 1
    allowed_operations: [command]
"""
COMMAND = """\
schema_version: 1
name: command
bench: basic-pair
steps:
  - action: send_command
    target: ext
    address: 100
    command: single
    value: true
    timeout: 5s
"""
MANAGED_COMMAND = COMMAND.replace("target: ext", "target: client-1")


class FakeConfirmer:
    """A confirmer that says whether elicitation is supported, and answers as told."""

    def __init__(self, *, supported: bool, answer: bool = True) -> None:
        self._supported = supported
        self._answer = answer
        self.questions: list[str] = []

    def supported(self) -> bool:
        return self._supported

    async def ask(self, question: str) -> tuple[bool, str]:
        self.questions.append(question)
        return self._answer, "answered by the test"


def bench_with(fragment: str) -> str:
    """The basic pair, with the external targets of `fragment` on a wan network."""
    data = load_document(BENCH)
    data["networks"].append({"name": "wan", "external": True})
    data["external_targets"] = load_document(fragment)["external_targets"]
    # A date written without quotes is read as a date: write it back as its text.
    return json.dumps(data, default=lambda value: value.isoformat())


def scenario_on(
    tmp_path: Path, bench_text: str, scenario_text: str = COMMAND
) -> tuple[ProjectStore, RunStore]:
    store = ProjectStore(tmp_path)
    runs = RunStore(tmp_path)
    service.create(store, "bench", "basic-pair", bench_text)
    service.create(store, "scenario", "command", scenario_text)
    return store, runs


def verdict_of(runs: RunStore, run_id: str) -> dict[str, Any]:
    verdicts = [
        e for e in runs.events(run_id, limit=100).events if e["kind"] == "verdict"
    ]
    assert len(verdicts) == 1
    return verdicts[0]


def confirmation_of(runs: RunStore, run_id: str) -> dict[str, Any]:
    found = [
        e for e in runs.events(run_id, limit=100).events if e["kind"] == "confirmation"
    ]
    assert len(found) == 1
    return found[0]


async def test_a_command_confirmed_by_a_person_is_recorded_as_confirmed(
    tmp_path: Path,
) -> None:
    store, runs = scenario_on(tmp_path, bench_with(EXTERNAL))
    confirmer = FakeConfirmer(supported=True, answer=True)
    runner = Runner(store, runs, FakeEngine())
    started = await runner.start("command", confirmer=confirmer)
    record = await settled(runner, started.id)
    assert confirmer.questions, "the person was asked"
    confirmed = confirmation_of(runs, started.id)
    assert confirmed["accepted"] is True
    assert confirmed["how"] == "elicitation"
    assert confirmed["target"] == "ext"
    assert confirmed["operation"] == "command"
    # Confirmed, but not carried out: the planner does not connect external targets yet.
    assert verdict_of(runs, started.id)["outcome"] == "unsupported"
    assert record.status is RunStatus.ERROR


async def test_a_command_declined_by_a_person_fails_and_is_recorded(
    tmp_path: Path,
) -> None:
    store, runs = scenario_on(tmp_path, bench_with(EXTERNAL))
    runner = Runner(store, runs, FakeEngine())
    started = await runner.start(
        "command", confirmer=FakeConfirmer(supported=True, answer=False)
    )
    record = await settled(runner, started.id)
    declined = confirmation_of(runs, started.id)
    assert declined["accepted"] is False
    assert declined["how"] == "elicitation"
    verdict = verdict_of(runs, started.id)
    assert verdict["outcome"] == "failed"
    assert "was not confirmed" in verdict["detail"]
    assert record.status is RunStatus.FAILED


async def test_without_elicitation_a_reviewed_opt_in_confirms_the_command(
    tmp_path: Path,
) -> None:
    opt_in = EXTERNAL + (
        "    opt_in:\n"
        "      reviewer: Reviewer One\n"
        "      reviewed_on: 2026-10-01\n"
        "      operations: [command]\n"
    )
    store, runs = scenario_on(tmp_path, bench_with(opt_in))
    confirmer = FakeConfirmer(supported=False)
    runner = Runner(store, runs, FakeEngine())
    started = await runner.start("command", confirmer=confirmer)
    await settled(runner, started.id)
    assert confirmer.questions == [], "no person was asked: the client cannot ask"
    confirmed = confirmation_of(runs, started.id)
    assert confirmed["accepted"] is True
    assert confirmed["how"] == "definition"
    assert "Reviewer One" in confirmed["detail"]


async def test_without_elicitation_or_an_opt_in_the_command_is_refused(
    tmp_path: Path,
) -> None:
    store, runs = scenario_on(tmp_path, bench_with(EXTERNAL))
    runner = Runner(store, runs, FakeEngine())
    started = await runner.start("command", confirmer=None)
    await settled(runner, started.id)
    refused = confirmation_of(runs, started.id)
    assert refused["accepted"] is False
    assert refused["how"] == "none"
    assert verdict_of(runs, started.id)["outcome"] == "failed"


async def test_a_client_that_supports_elicitation_is_asked_even_with_an_opt_in(
    tmp_path: Path,
) -> None:
    opt_in = EXTERNAL + (
        "    opt_in:\n"
        "      reviewer: Reviewer One\n"
        "      reviewed_on: 2026-10-01\n"
        "      operations: [command]\n"
    )
    store, runs = scenario_on(tmp_path, bench_with(opt_in))
    confirmer = FakeConfirmer(supported=True, answer=False)
    runner = Runner(store, runs, FakeEngine())
    started = await runner.start("command", confirmer=confirmer)
    await settled(runner, started.id)
    assert len(confirmer.questions) == 1
    assert confirmation_of(runs, started.id)["how"] == "elicitation"


async def test_a_command_to_a_managed_client_is_not_confirmed_and_is_unsupported(
    tmp_path: Path,
) -> None:
    store, runs = scenario_on(tmp_path, BENCH, MANAGED_COMMAND)
    confirmer = FakeConfirmer(supported=True)
    runner = Runner(store, runs, FakeEngine())
    started = await runner.start("command", confirmer=confirmer)
    record = await settled(runner, started.id)
    assert confirmer.questions == []
    assert "confirmation" not in kinds(runs, started.id)
    assert verdict_of(runs, started.id)["outcome"] == "unsupported"
    assert record.status is RunStatus.ERROR


async def test_a_command_to_a_target_that_does_not_allow_it_is_refused_at_start(
    tmp_path: Path,
) -> None:
    refusing = EXTERNAL.replace(
        "allowed_operations: [command]", "allowed_operations: []"
    )
    store, runs = scenario_on(tmp_path, bench_with(refusing))
    runner = Runner(store, runs, FakeEngine())
    with pytest.raises(ServiceError) as error:
        await runner.start("command", confirmer=FakeConfirmer(supported=True))
    assert error.value.code == "invalid_definition"
    assert runs.list_runs(limit=10).items == []


def target_with(
    opt_in: dict[str, Any] | None = None, allowed: list[str] | None = None
) -> dict[str, Any]:
    target: dict[str, Any] = {
        "name": "ext",
        "role": "client",
        "network": "wan",
        "endpoint": {"host": "127.0.0.1", "port": 2404},
        "common_address": 1,
        "allowed_operations": ["command"] if allowed is None else allowed,
    }
    if opt_in is not None:
        target["opt_in"] = opt_in
    return target


REVIEWED = {"reviewer": "R", "reviewed_on": "2026-10-01", "operations": ["command"]}


def test_an_opt_in_covers_only_an_allowed_state_changing_operation() -> None:
    ExternalTarget.model_validate(target_with(REVIEWED))
    with pytest.raises(ValueError, match="does not allow"):
        ExternalTarget.model_validate(target_with(REVIEWED, allowed=[]))
    reads = {**REVIEWED, "operations": ["read"]}
    with pytest.raises(ValueError, match="does not change state"):
        ExternalTarget.model_validate(target_with(reads, allowed=["read"]))


def test_an_opt_in_needs_a_review_date_and_a_reviewer() -> None:
    bad_date = {**REVIEWED, "reviewed_on": "1 October"}
    no_reviewer = {**REVIEWED, "reviewer": ""}
    for bad in (bad_date, no_reviewer):
        with pytest.raises(ValueError):
            ExternalTarget.model_validate(target_with(bad))


OPT_IN_YAML = (
    "    opt_in:\n"
    "      reviewer: Reviewer One\n"
    "      reviewed_on: 2026-10-01\n"
    "      operations: [command]\n"
)


def test_a_bench_with_an_opt_in_loads_and_keeps_its_reviewer() -> None:
    bench = load_bench(bench_with(EXTERNAL + OPT_IN_YAML))
    opt_in = bench.external_targets[0].opt_in
    assert opt_in is not None
    assert opt_in.reviewer == "Reviewer One"


def test_a_bench_whose_opt_in_names_an_unlisted_operation_does_not_load() -> None:
    unlisted = EXTERNAL.replace(
        "allowed_operations: [command]", "allowed_operations: []"
    )
    with pytest.raises(Exception, match="does not allow"):
        load_bench(bench_with(unlisted + OPT_IN_YAML))


class PeerStub:
    """A session that declares elicitation or not, and answers the question as told."""

    def __init__(self, *, elicitation: bool, action: str, confirm: bool) -> None:
        self.elicitation = elicitation
        self.result = ElicitResult(action=action, content={"confirm": confirm})  # type: ignore[arg-type]

    def check_client_capability(self, capability: ClientCapabilities) -> bool:
        return self.elicitation

    async def elicit_form(self, message: str, requested_schema: Any) -> ElicitResult:
        return self.result


@pytest.mark.parametrize(
    ("action", "confirm", "expected"),
    [
        ("accept", True, True),
        ("accept", False, False),
        ("decline", True, False),
        ("cancel", True, False),
    ],
)
async def test_an_elicitation_answer_is_a_yes_only_when_accepted_with_confirm(
    action: str, confirm: bool, expected: bool
) -> None:
    confirmer = ElicitingConfirmer(
        PeerStub(elicitation=True, action=action, confirm=confirm)
    )
    assert confirmer.supported() is True
    accepted, _ = await confirmer.ask("send?")
    assert accepted is expected


def test_a_client_without_elicitation_is_not_asked() -> None:
    confirmer = ElicitingConfirmer(
        PeerStub(elicitation=False, action="accept", confirm=True)
    )
    assert confirmer.supported() is False


class SilentPeer(PeerStub):
    """A session that declares elicitation, and whose client never answers."""

    async def elicit_form(self, message: str, requested_schema: Any) -> ElicitResult:
        raise ConnectionError("the client went away")


async def test_a_client_that_does_not_answer_confirms_nothing(tmp_path: Path) -> None:
    store, runs = scenario_on(tmp_path, bench_with(EXTERNAL))
    peer = SilentPeer(elicitation=True, action="accept", confirm=True)
    runner = Runner(store, runs, FakeEngine())
    started = await runner.start("command", confirmer=ElicitingConfirmer(peer))
    await settled(runner, started.id)
    unanswered = confirmation_of(runs, started.id)
    assert unanswered["accepted"] is False
    assert "did not answer" in unanswered["detail"]
    assert verdict_of(runs, started.id)["outcome"] == "failed"
