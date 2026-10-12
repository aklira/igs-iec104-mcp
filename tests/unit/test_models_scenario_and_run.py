"""Scenarios against their bench, and runs with their snapshots (§5, §8)."""

import copy
from typing import Any

import pytest
from bench_data import BENCH
from pydantic import ValidationError

from igs_iec104_mcp.models.bench import TestBench as Bench
from igs_iec104_mcp.models.common import DefinitionError
from igs_iec104_mcp.models.loader import load_scenario
from igs_iec104_mcp.models.run import Run, snapshot_of
from igs_iec104_mcp.models.scenario import Scenario, check_scenario

SCENARIO: dict[str, Any] = {
    "schema_version": 1,
    "name": "smoke",
    "bench": "basic-pair",
    "steps": [
        {"action": "wait_transfer_started", "client": "client-1", "timeout": "10s"},
        {"action": "general_interrogation", "client": "client-1"},
        {
            "action": "assert_point",
            "client": "client-1",
            "address": 100,
            "kind": "float",
            "value": 12.5,
        },
        {"action": "update_point", "server": "server-1", "address": 100, "value": 18.0},
        {
            "action": "expect_point",
            "client": "client-1",
            "address": 100,
            "kind": "float",
            "value": 18.0,
            "timeout": "2s",
        },
    ],
}


def bench() -> Bench:
    return Bench.model_validate(copy.deepcopy(BENCH))


def scenario(**changes: Any) -> Scenario:
    data = copy.deepcopy(SCENARIO)
    data.update(changes)
    return Scenario.model_validate(data)


def problems(data: dict[str, Any], target: Bench | None = None) -> list[str]:
    model = Scenario.model_validate(data)
    with pytest.raises(DefinitionError) as error:
        check_scenario(model, target or bench())
    return [str(issue) for issue in error.value.issues]


def test_the_smoke_scenario_is_valid_against_its_bench() -> None:
    check_scenario(scenario(), bench())


def test_an_action_outside_the_list_is_refused_with_the_actions() -> None:
    data = copy.deepcopy(SCENARIO)
    data["steps"].append({"action": "run_shell", "command": "ls"})
    with pytest.raises(
        ValidationError, match="unsupported action 'run_shell': the actions are"
    ):
        Scenario.model_validate(data)


def test_an_unknown_action_inside_a_parallel_branch_is_refused() -> None:
    data = copy.deepcopy(SCENARIO)
    data["steps"] = [{"action": "parallel", "branches": [[{"action": "exec"}]]}]
    with pytest.raises(ValidationError, match="unsupported action 'exec'"):
        Scenario.model_validate(data)


def test_a_timeout_that_is_not_a_duration_is_refused() -> None:
    data = copy.deepcopy(SCENARIO)
    data["steps"][0]["timeout"] = "ten seconds"
    with pytest.raises(ValidationError, match="is not a duration"):
        Scenario.model_validate(data)


def test_a_zero_timeout_is_refused() -> None:
    data = copy.deepcopy(SCENARIO)
    data["steps"][0]["timeout"] = "0ms"
    with pytest.raises(ValidationError, match="must be a duration above zero"):
        Scenario.model_validate(data)


def test_a_global_timeout_longer_than_a_day_is_refused() -> None:
    with pytest.raises(ValidationError, match="longer than a day"):
        scenario(global_timeout="25h")


def test_a_scenario_on_another_bench_is_refused() -> None:
    assert any(
        "the scenario runs on 'other'" in p
        for p in problems(scenario(bench="other").model_dump(mode="json"))
    )


def test_a_step_naming_an_unknown_instance_is_refused() -> None:
    data = copy.deepcopy(SCENARIO)
    data["steps"][1]["client"] = "client-9"
    assert any("'client-9' is not an instance" in p for p in problems(data))


def test_a_step_naming_a_server_as_a_client_is_refused() -> None:
    data = copy.deepcopy(SCENARIO)
    data["steps"][1]["client"] = "server-1"
    assert any(
        "is a server, and only a client has an image" in p for p in problems(data)
    )


def test_an_update_to_a_point_that_does_not_exist_is_refused() -> None:
    data = copy.deepcopy(SCENARIO)
    data["steps"][3]["address"] = 101
    assert any("has no point at address 101" in p for p in problems(data))


def test_an_update_with_a_value_of_the_wrong_kind_is_refused() -> None:
    data = copy.deepcopy(SCENARIO)
    data["steps"][3]["value"] = True
    assert any("a float point takes a finite number" in p for p in problems(data))


def test_a_command_to_a_target_that_does_not_allow_it_is_refused() -> None:
    target_bench = copy.deepcopy(BENCH)
    target_bench["external_targets"] = [
        {
            "name": "plant",
            "role": "client",
            "network": "wan",
            "endpoint": {"host": "10.0.0.9", "port": 2404},
            "common_address": 1,
            "allowed_operations": ["interrogate"],
        }
    ]
    data = copy.deepcopy(SCENARIO)
    data["steps"] = [
        {
            "action": "send_command",
            "target": "plant",
            "address": 100,
            "command": "single",
            "value": True,
        }
    ]
    found = problems(data, Bench.model_validate(target_bench))
    assert any(
        "does not allow the 'command' operation (allowed: interrogate)" in p
        for p in found
    )


def test_a_single_command_takes_a_boolean_and_a_double_command_on_or_off() -> None:
    with pytest.raises(ValidationError, match="a single command takes true or false"):
        Scenario.model_validate(
            {
                **SCENARIO,
                "steps": [
                    {
                        "action": "send_command",
                        "target": "client-1",
                        "address": 1,
                        "command": "single",
                        "value": "on",
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match='takes "on" or "off"'):
        Scenario.model_validate(
            {
                **SCENARIO,
                "steps": [
                    {
                        "action": "send_command",
                        "target": "client-1",
                        "address": 1,
                        "command": "double",
                        "value": True,
                    }
                ],
            }
        )


def test_an_external_target_cannot_be_stopped_or_started() -> None:
    target_bench = copy.deepcopy(BENCH)
    target_bench["external_targets"] = [
        {
            "name": "plant",
            "role": "server",
            "network": "wan",
            "endpoint": {"host": "10.0.0.9", "port": 2404},
            "common_address": 1,
        }
    ]
    data = copy.deepcopy(SCENARIO)
    data["steps"] = [{"action": "stop_instance", "instance": "plant"}]
    found = problems(data, Bench.model_validate(target_bench))
    assert any("the bench does not manage" in p for p in found)


def group_bench() -> Bench:
    """The bench with a second station, and the client connected to both."""
    data = copy.deepcopy(BENCH)
    station = next(i for i in data["instances"] if i["name"] == "server-1")
    data["instances"].append({**station, "name": "server-2", "common_address": 2})
    data["connections"].append({"client": "client-1", "server": "server-2"})
    return Bench.model_validate(data)


def test_a_switchover_needs_a_client_of_a_redundancy_group() -> None:
    data = copy.deepcopy(SCENARIO)
    data["steps"] = [{"action": "switchover", "client": "client-1"}]
    found = problems(data)
    assert any("needs a redundancy group" in p for p in found)
    data["steps"] = [{"action": "switchover", "client": "server-1"}]
    assert any("is a server: a switchover is of a client" in p for p in problems(data))


def test_a_switchover_of_a_group_is_valid() -> None:
    data = copy.deepcopy(SCENARIO)
    data["steps"] = [{"action": "switchover", "client": "client-1"}]
    check_scenario(Scenario.model_validate(data), group_bench())


def test_a_parallel_branch_with_no_step_is_refused() -> None:
    data = copy.deepcopy(SCENARIO)
    data["steps"] = [{"action": "parallel", "branches": [[]]}]
    with pytest.raises(ValidationError, match="has no steps"):
        Scenario.model_validate(data)


def test_a_scenario_file_with_an_unknown_field_is_refused_with_its_path() -> None:
    text = "schema_version: 1\nname: x\nbench: basic-pair\nsteps: []\nretries: 3\n"
    with pytest.raises(DefinitionError) as error:
        load_scenario(text)
    assert any(issue.path == "retries" for issue in error.value.issues)


def _run(
    bench_data: dict[str, Any], scenario_data: dict[str, Any], **changes: Any
) -> dict[str, Any]:
    snapshot = snapshot_of(
        Bench.model_validate(bench_data), Scenario.model_validate(scenario_data)
    )
    run = {
        "schema_version": 1,
        "id": "0123456789abcdef0123456789abcdef",
        "status": "passed",
        "started_at_ms": 1000,
        "ended_at_ms": 2000,
        "snapshot": snapshot.model_dump(mode="json"),
        "progress": {"completed": 5, "total": 5},
        "observations": [],
        "verdicts": [{"step": "steps[0]", "outcome": "passed", "detail": ""}],
        "artifacts": [],
    }
    run.update(changes)
    return run


def test_a_finished_run_with_its_snapshot_is_valid() -> None:
    run = Run.model_validate(_run(copy.deepcopy(BENCH), copy.deepcopy(SCENARIO)))
    assert run.status == "passed"


def test_a_snapshot_changed_after_the_run_is_refused() -> None:
    data = _run(copy.deepcopy(BENCH), copy.deepcopy(SCENARIO))
    data["snapshot"]["scenario"]["steps"][0]["timeout"] = "99s"
    with pytest.raises(ValidationError, match="the snapshot was changed"):
        Run.model_validate(data)


def test_a_run_that_ended_needs_its_end_time_and_a_running_one_has_none() -> None:
    with pytest.raises(ValidationError, match="needs 'ended_at_ms'"):
        Run.model_validate(
            _run(copy.deepcopy(BENCH), copy.deepcopy(SCENARIO), ended_at_ms=None)
        )
    with pytest.raises(ValidationError, match="must be absent"):
        Run.model_validate(
            _run(copy.deepcopy(BENCH), copy.deepcopy(SCENARIO), status="running")
        )


def test_progress_cannot_exceed_its_total() -> None:
    with pytest.raises(ValidationError, match="more than the total"):
        Run.model_validate(
            _run(
                copy.deepcopy(BENCH),
                copy.deepcopy(SCENARIO),
                progress={"completed": 6, "total": 5},
            )
        )


def test_a_passed_run_with_a_failed_step_is_refused() -> None:
    verdicts = [{"step": "steps[0]", "outcome": "timed_out", "detail": "no answer"}]
    with pytest.raises(ValidationError, match="must be failed"):
        Run.model_validate(
            _run(copy.deepcopy(BENCH), copy.deepcopy(SCENARIO), verdicts=verdicts)
        )
