"""The switchover and the stop of a managed station on Docker (IMPLEMENTATION.md §8,
§10; task V2c).

The group bench is the basic pair with a second station, and the client connects to
both. A scenario stops the station its group started: the group moves the transfer to
the other station, and the client interrogates it and reads its value. Another scenario
stops whichever station the group has not started, or the only station of the basic
pair: each stopped station keeps its logs as evidence, and a restart is still refused.
Each run removes its bench. The images are named as for the other Docker tests.
"""

import json
from pathlib import Path
from typing import Any

from test_docker_group import group_bench, identity
from test_docker_tls_scenarios import journal_of, settle

from igs_iec104_mcp.definitions import service
from igs_iec104_mcp.definitions.store import ProjectStore
from igs_iec104_mcp.docker.adapter import DockerAdapter
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.scenarios.runner import PROJECT_LABEL, DockerEngine, Runner
from igs_iec104_mcp.storage.runs import RunStore

ROOT = Path(__file__).resolve().parents[2]
BASIC_PAIR = ROOT / "examples/benches/basic-pair.yaml"
GROUP = "group-pair"
SWITCHOVER = """\
schema_version: 1
name: switchover
bench: group-pair
steps:
  - action: wait_connection
    instance: client-1
    timeout: 60s
  - action: wait_transfer_started
    client: client-1
    timeout: 60s
  - action: switchover
    client: client-1
    timeout: 60s
  - action: general_interrogation
    client: client-1
    timeout: 20s
  - action: assert_point
    client: client-1
    address: 100
    kind: float
    value: 12.5
    tolerance: 0.001
"""
STOP_A_STATION = """\
schema_version: 1
name: stop-station
bench: group-pair
steps:
  - action: wait_connection
    instance: client-1
    timeout: 60s
  - action: wait_transfer_started
    client: client-1
    timeout: 60s
  - action: stop_instance
    instance: server-1
    timeout: 60s
  - action: wait_transfer_started
    client: client-1
    timeout: 60s
  - action: general_interrogation
    client: client-1
    timeout: 20s
  - action: assert_point
    client: client-1
    address: 100
    kind: float
    value: 12.5
    tolerance: 0.001
"""
STOP_ONLY = """\
schema_version: 1
name: stop-only
bench: basic-pair
steps:
  - action: wait_connection
    instance: client-1
    timeout: 60s
  - action: stop_instance
    instance: server-1
    timeout: 60s
"""
RESTART = """\
schema_version: 1
name: restart
bench: basic-pair
steps:
  - action: stop_instance
    instance: server-1
    timeout: 60s
  - action: start_instance
    instance: server-1
    timeout: 10s
"""


def named_group_bench() -> str:
    """The group bench under the name the scenarios use. Its second station answers to
    the client's common address, as the two stations of a redundancy pair do (§10): the
    client's requests to a station of another address are refused, and the group bench
    of V2b gave the second station address 2."""
    data = json.loads(group_bench())
    data["name"] = GROUP
    for instance in data["instances"]:
        if instance["name"] == "server-2":
            instance["common_address"] = 1
    return json.dumps(data)


def runner_for(tmp_path: Path) -> tuple[Runner, ProjectStore, Path]:
    project = tmp_path / "project"
    project.mkdir()
    store = ProjectStore(project)
    engine = DockerEngine(
        image=identity("IGS_RUNTIME_IMAGE", "docker/runtime.Dockerfile"),
        relay=identity("IGS_RELAY_IMAGE", "docker/relay.Dockerfile"),
    )
    return Runner(store, RunStore(project), engine), store, project


def verdicts_of(runner: Runner, run_id: str) -> list[dict[str, Any]]:
    return [e for e in journal_of(runner, run_id) if e["kind"] == "verdict"]


def evidence_of(project: Path, run_id: str) -> list[str]:
    evidence = project / "runs" / run_id / "evidence"
    return sorted(path.name for path in evidence.iterdir()) if evidence.exists() else []


async def test_a_switchover_moves_the_transfer_to_the_other_station(
    tmp_path: Path,
) -> None:
    runner, store, project = runner_for(tmp_path)
    service.create(store, "bench", GROUP, named_group_bench())
    service.create(store, "scenario", "switchover", SWITCHOVER)
    started = await runner.start("switchover")
    status = await settle(runner, started.id)
    assert status == "passed", journal_of(runner, started.id)
    verdicts = verdicts_of(runner, started.id)
    assert [v["outcome"] for v in verdicts] == ["passed"] * 5
    assert "switched from" in str(verdicts[2]["detail"])
    stopped = [
        n for n in evidence_of(project, started.id) if n.endswith("-stopped.log")
    ]
    assert len(stopped) == 1, "only the started station is stopped, and it keeps logs"
    owner = Ownership(project=PROJECT_LABEL, bench=GROUP, run=started.id)
    assert await DockerAdapter.connect().owned(owner) == [], "the bench was removed"


async def test_a_stop_of_a_group_station_keeps_the_group_serving(
    tmp_path: Path,
) -> None:
    runner, store, _ = runner_for(tmp_path)
    service.create(store, "bench", GROUP, named_group_bench())
    service.create(store, "scenario", "stop-station", STOP_A_STATION)
    started = await runner.start("stop-station")
    status = await settle(runner, started.id)
    assert status == "passed", journal_of(runner, started.id)
    outcomes = [v["outcome"] for v in verdicts_of(runner, started.id)]
    assert outcomes == ["passed"] * 6


async def test_a_stop_keeps_the_station_logs_and_a_restart_is_refused(
    tmp_path: Path,
) -> None:
    runner, store, project = runner_for(tmp_path)
    service.create(store, "bench", "basic-pair", BASIC_PAIR.read_text(encoding="utf-8"))
    service.create(store, "scenario", "stop-only", STOP_ONLY)
    service.create(store, "scenario", "restart", RESTART)

    stopped = await runner.start("stop-only")
    assert await settle(runner, stopped.id) == "passed"
    assert "server-1-stopped.log" in evidence_of(project, stopped.id)

    restarted = await runner.start("restart")
    assert await settle(runner, restarted.id) == "error"
    refusal = [
        v
        for v in verdicts_of(runner, restarted.id)
        if v["step"].endswith("start_instance")
    ]
    assert refusal and "cannot be started on a managed bench" in str(
        refusal[0]["detail"]
    )
    owner = Ownership(project=PROJECT_LABEL, bench="basic-pair", run=restarted.id)
    assert await DockerAdapter.connect().owned(owner) == [], "the bench was removed"
