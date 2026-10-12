"""The fault scenarios on Docker (IMPLEMENTATION.md §8, §13; task V3c).

The basic pair's connection goes through a fault proxy. Each scenario runs through the
run engine, with its steps on the real proxy:

- an interruption: the proxy closes the client's connection and refuses the
  reconnections; the client records the loss; the fault is cleared; the client
  reconnects through the proxy, starts its transfer again, interrogates, and reads the
  station's value;
- a latency: each chunk the proxy forwards waits 200 ms; the interrogation still passes,
  and it takes at least the delay of its exchange; the fault is cleared at the end.

Each run removes its bench, and keeps the proxy's logs as evidence. The images are named
as for the other Docker tests.
"""

import asyncio
from pathlib import Path
from typing import Any

from test_docker_faults import proxied_bench
from test_docker_switchover import evidence_of, runner_for
from test_docker_tls_scenarios import journal_of, settle

from igs_iec104_mcp.definitions import service

INTERRUPT_AND_RECONNECT = """\
schema_version: 1
name: interrupt-and-reconnect
bench: proxied-pair
steps:
  - action: wait_connection
    instance: client-1
    timeout: 60s
  - action: start_transfer
    client: client-1
    timeout: 20s
  - action: inject_fault
    client: client-1
    server: server-1
    mode: interrupt
    timeout: 30s
  - action: clear_fault
    client: client-1
    server: server-1
    timeout: 10s
  - action: wait_connection
    instance: client-1
    timeout: 90s
  - action: start_transfer
    client: client-1
    timeout: 20s
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
LATENCY_INTERROGATION = """\
schema_version: 1
name: latency-interrogation
bench: proxied-pair
steps:
  - action: wait_connection
    instance: client-1
    timeout: 60s
  - action: inject_fault
    client: client-1
    server: server-1
    mode: latency
    latency_ms: 200
    timeout: 10s
  - action: start_transfer
    client: client-1
    timeout: 30s
  - action: general_interrogation
    client: client-1
    timeout: 30s
  - action: assert_point
    client: client-1
    address: 100
    kind: float
    value: 12.5
    tolerance: 0.001
  - action: clear_fault
    client: client-1
    server: server-1
    timeout: 10s
"""
RUN_PATIENCE = 240.0


def verdicts(runner: Any, run_id: str) -> list[dict[str, Any]]:
    return [e for e in journal_of(runner, run_id) if e["kind"] == "verdict"]


async def test_an_interrupted_client_reconnects_through_the_proxy_after_the_clear(
    tmp_path: Path,
) -> None:
    runner, store, project = runner_for(tmp_path)
    service.create(store, "bench", "proxied-pair", proxied_bench())
    service.create(
        store, "scenario", "interrupt-and-reconnect", INTERRUPT_AND_RECONNECT
    )
    started = await runner.start("interrupt-and-reconnect")
    async with asyncio.timeout(RUN_PATIENCE):
        status = await settle(runner, started.id)
    assert status == "passed", journal_of(runner, started.id)
    outcomes = [v["outcome"] for v in verdicts(runner, started.id)]
    assert outcomes == ["passed"] * 8
    details = [v["detail"] for v in verdicts(runner, started.id)]
    assert "(interrupt)" in details[2], details
    assert "no fault" in details[3], details
    assert "proxy-1.log" in evidence_of(project, started.id)


async def test_a_latency_delays_the_interrogation_and_the_fault_is_cleared(
    tmp_path: Path,
) -> None:
    runner, store, project = runner_for(tmp_path)
    service.create(store, "bench", "proxied-pair", proxied_bench())
    service.create(store, "scenario", "latency-interrogation", LATENCY_INTERROGATION)
    started = await runner.start("latency-interrogation")
    async with asyncio.timeout(RUN_PATIENCE):
        status = await settle(runner, started.id)
    assert status == "passed", journal_of(runner, started.id)
    found = verdicts(runner, started.id)
    assert [v["outcome"] for v in found] == ["passed"] * 6
    interrogation = next(v for v in found if "general_interrogation" in v["step"])
    assert float(interrogation["seconds"]) >= 0.2, (
        "each chunk of the exchange waited for the latency"
    )
    assert "proxy-1.log" in evidence_of(project, started.id)
