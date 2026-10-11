"""The test bench's lifecycle against a real engine (IMPLEMENTATION.md §6; task D2): the
basic pair comes up, each runtime is ready, a client is connected to its station, the
stop is graceful and keeps the evidence, a failure at every step leaves no owned
resource, and two benches run side by side.

The runtime image is named by IGS_RUNTIME_IMAGE (a tag or an image identity), built from
docker/runtime.Dockerfile. Without it the tests skip, except in CI, where a missing
image fails the run."""

import asyncio
import os
import secrets
from pathlib import Path
from typing import Any

import pytest
from docker.errors import NotFound

from docker import from_env  # type: ignore[attr-defined]
from igs_iec104_mcp.docker.adapter import DockerAdapter
from igs_iec104_mcp.docker.bench import RunningBench, start_bench
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.docker.plan import PlanError
from igs_iec104_mcp.models.bench import TestBench
from igs_iec104_mcp.models.loader import load_bench, load_document

ROOT = Path(__file__).resolve().parents[2]
BASIC_PAIR = ROOT / "examples/benches/basic-pair.yaml"
PROJECT = "igs-test"


class Injected(Exception):
    """The failure a test injects at one step of a start."""


@pytest.fixture
def runtime_image() -> str:
    """The identity of the runtime image named by IGS_RUNTIME_IMAGE."""
    reference = os.environ.get("IGS_RUNTIME_IMAGE")
    message = "set IGS_RUNTIME_IMAGE to the runtime image (docker/runtime.Dockerfile)"
    if not reference:
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    try:
        identity: str = from_env().images.get(reference).id
    except NotFound:
        pytest.fail(f"no image {reference!r} on the engine: {message}")
    return identity


@pytest.fixture
def relay_image() -> str:
    """The identity of the relay image named by IGS_RELAY_IMAGE."""
    reference = os.environ.get("IGS_RELAY_IMAGE")
    message = "set IGS_RELAY_IMAGE to the relay image (docker/relay.Dockerfile)"
    if not reference:
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    try:
        identity: str = from_env().images.get(reference).id
    except NotFound:
        pytest.fail(f"no image {reference!r} on the engine: {message}")
    return identity


@pytest.fixture
def bench_run_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs"


def basic_pair() -> TestBench:
    return load_bench(BASIC_PAIR.read_text(encoding="utf-8"))


def basic_pair_data() -> dict[str, Any]:
    data: dict[str, Any] = load_document(BASIC_PAIR.read_text(encoding="utf-8"))
    return data


def new_ownership() -> Ownership:
    return Ownership(project=PROJECT, bench="basic-pair", run=secrets.token_hex(16))


async def connected(running: RunningBench, instance: str) -> dict[str, Any]:
    client = running.client(instance)
    try:
        return await client.ready()
    finally:
        await client.aclose()


async def test_the_basic_pair_comes_up_and_stops_with_its_evidence(
    adapter: DockerAdapter, runtime_image: str, relay_image: str, bench_run_dir: Path
) -> None:
    run = new_ownership()
    evidence = bench_run_dir / run.run
    running = await start_bench(
        adapter,
        basic_pair(),
        project=PROJECT,
        run=run.run,
        image=runtime_image,
        relay=relay_image,
        evidence=evidence,
    )
    try:
        station = await connected(running, "server-1")
        client = await connected(running, "client-1")
        assert station["listen_address"] == running.members["server-1"].listen
        assert station["listen_address"].startswith("0.0.0.0:")
        assert client["connection"] == "up"
        assert running.members["server-1"].control.startswith("127.0.0.1:")
        runtime = await adapter.inspect(running.members["server-1"].resource)
        published = runtime["NetworkSettings"].get("Ports") or {}
        assert not any(published.values()), "a runtime publishes no port"
        assert len(await adapter.owned(run)) == 6
    finally:
        report = await running.stop(grace=10)

    assert report.graceful, report.exit_codes
    assert report.exit_codes == {"client-1": 0, "server-1": 0}
    assert report.removed == 6
    assert await adapter.owned(run) == []
    kept = {path.name for path in evidence.iterdir()}
    assert {"server-1.log", "client-1.log", "relay-server-1.log", "stop.json"} <= kept
    for path in evidence.iterdir():
        assert running.token not in path.read_text(
            encoding="utf-8", errors="replace"
        ), f"the control token is in {path.name}"


async def test_a_failure_at_each_step_leaves_no_owned_resource(
    adapter: DockerAdapter, runtime_image: str, relay_image: str, bench_run_dir: Path
) -> None:
    steps: list[str] = []

    async def record(name: str) -> None:
        steps.append(name)

    first = new_ownership()
    running = await start_bench(
        adapter,
        basic_pair(),
        project=PROJECT,
        run=first.run,
        image=runtime_image,
        relay=relay_image,
        evidence=bench_run_dir / first.run,
        checkpoint=record,
    )
    await running.stop(grace=10)
    assert steps == [
        "image",
        "network:lan",
        "network:control",
        "create:server-1",
        "start:server-1",
        "relay:server-1",
        "ready:server-1",
        "create:client-1",
        "start:client-1",
        "relay:client-1",
        "ready:client-1",
        "connect:client-1",
    ]

    for step in steps:
        run = new_ownership()
        evidence = bench_run_dir / run.run

        async def fail(name: str, target: str = step) -> None:
            if name == target:
                raise Injected(name)

        with pytest.raises(Injected):
            await start_bench(
                adapter,
                basic_pair(),
                project=PROJECT,
                run=run.run,
                image=runtime_image,
                relay=relay_image,
                evidence=evidence,
                checkpoint=fail,
            )
        assert await adapter.owned(run) == [], f"a failure at {step} left resources"
        if steps.index(step) > steps.index("ready:server-1"):
            assert (evidence / "server-1.log").is_file(), f"no evidence at {step}"


async def test_two_benches_run_side_by_side(
    adapter: DockerAdapter, runtime_image: str, relay_image: str, bench_run_dir: Path
) -> None:
    first, second = new_ownership(), new_ownership()
    bench = basic_pair()
    one, two = await asyncio.gather(
        start_bench(
            adapter,
            bench,
            project=PROJECT,
            run=first.run,
            image=runtime_image,
            relay=relay_image,
            evidence=bench_run_dir / first.run,
        ),
        start_bench(
            adapter,
            bench,
            project=PROJECT,
            run=second.run,
            image=runtime_image,
            relay=relay_image,
            evidence=bench_run_dir / second.run,
        ),
    )
    try:
        owned_first = {resource.identity for resource in await adapter.owned(first)}
        owned_second = {resource.identity for resource in await adapter.owned(second)}
        assert owned_first and owned_second
        assert owned_first.isdisjoint(owned_second)
        assert (await connected(one, "client-1"))["connection"] == "up"
        assert (await connected(two, "client-1"))["connection"] == "up"
    finally:
        await one.stop(grace=10)
        assert await adapter.owned(first) == []
        assert len(await adapter.owned(second)) == 6
        assert (await connected(two, "client-1"))["connection"] == "up"
        await two.stop(grace=10)
    assert await adapter.owned(second) == []


async def test_a_bench_that_does_not_plan_creates_nothing(
    adapter: DockerAdapter, runtime_image: str, relay_image: str, bench_run_dir: Path
) -> None:
    data = basic_pair_data()
    next(item for item in data["instances"] if item["name"] == "client-1")["limits"] = {
        "pids": 8
    }
    run = new_ownership()
    with pytest.raises(PlanError, match="pids"):
        await start_bench(
            adapter,
            TestBench.model_validate(data),
            project=PROJECT,
            run=run.run,
            image=runtime_image,
            relay=relay_image,
            evidence=bench_run_dir / run.run,
        )
    assert await adapter.owned(run) == []


async def test_non_default_link_parameters_reach_the_runtime(
    adapter: DockerAdapter, runtime_image: str, relay_image: str, bench_run_dir: Path
) -> None:
    """A bench whose client sets its link parameters starts. The runtime is given them
    as flags (D-025): they are in the container's command, and the link is up."""
    data = basic_pair_data()
    next(item for item in data["instances"] if item["name"] == "client-1")[
        "parameters"
    ] = {"t1": 20, "t2": 5, "k": 4}
    run = new_ownership()
    running = await start_bench(
        adapter,
        TestBench.model_validate(data),
        project=PROJECT,
        run=run.run,
        image=runtime_image,
        relay=relay_image,
        evidence=bench_run_dir / run.run,
    )
    try:
        client = await connected(running, "client-1")
        assert client["connection"] == "up"
        record = await adapter.inspect(running.members["client-1"].resource)
        command = record["Config"]["Cmd"]
        assert command[command.index("--t1") + 1] == "20"
        assert command[command.index("--t2") + 1] == "5"
        assert command[command.index("--k") + 1] == "4"
    finally:
        await running.stop(grace=10)
    assert await adapter.owned(run) == []
