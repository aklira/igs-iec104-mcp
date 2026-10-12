"""The TLS scenarios on Docker: an acceptance and a rejection, run by the run engine
(IMPLEMENTATION.md §7, §8, §13; task V1c).

The acceptance bench trusts its root on both sides: the client starts its transfer,
interrogates the station, and reads its value. The rejection bench is the same, except
that its station trusts another root, so it refuses the client's certificate. The
scenario expects that refusal: the station records a security alarm, and it accepts no
connection. Each run removes its bench. The images are named as for the other Docker
tests.
"""

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from test_docker_tls import identity, make_pki, tls_bench

from igs_iec104_mcp.definitions import service
from igs_iec104_mcp.definitions.secrets import SecretStore
from igs_iec104_mcp.definitions.store import ProjectStore
from igs_iec104_mcp.docker.adapter import DockerAdapter
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.models.run import TERMINAL
from igs_iec104_mcp.scenarios.runner import PROJECT_LABEL, DockerEngine, Runner
from igs_iec104_mcp.storage.runs import RunStore

ACCEPTED = """\
schema_version: 1
name: tls-accepted
bench: tls-pair
steps:
  - action: start_transfer
    client: client-1
    timeout: 10s
  - action: general_interrogation
    client: client-1
    timeout: 10s
  - action: assert_point
    client: client-1
    address: 100
    kind: float
    value: 12.5
    tolerance: 0.001
"""
REFUSED = """\
schema_version: 1
name: tls-refused
bench: tls-untrusted
steps:
  - action: expect_refusal
    client: client-1
    server: server-1
    timeout: 60s
"""
RUN_PATIENCE = 180.0
POLL_SECONDS = 0.2
# The secrets the benches name, and the DER file of the scratch PKI each one is.
SECRETS = {
    "station-cert": "station-cert.der",
    "station-key": "station-key.der",
    "client-cert": "client-cert.der",
    "client-key": "client-key.der",
    "root": "root.der",
    "other-root": "other-root.der",
}


def openssl(directory: Path, *args: str) -> None:
    subprocess.run(["openssl", *args], cwd=directory, check=True, capture_output=True)


def make_other_root(directory: Path) -> None:
    """A second root, which signs nothing the tests present."""
    openssl(
        directory,
        "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
        "-keyout", "other.key", "-out", "other.pem", "-subj", "/CN=other-root",
    )  # fmt: skip
    openssl(
        directory,
        "x509", "-in", "other.pem", "-outform", "DER", "-out", "other-root.der",
    )  # fmt: skip


def secrets_in(tmp_path: Path) -> Path:
    """The secrets directory: owner-only, outside the project, with the six secrets."""
    scratch = tmp_path / "pki"
    scratch.mkdir()
    make_pki(scratch)
    make_other_root(scratch)
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    secrets.chmod(0o700)
    for name, source in SECRETS.items():
        target = secrets / name
        target.write_bytes((scratch / source).read_bytes())
        target.chmod(0o600)
    return secrets


def bench_named(name: str, station_trust: str) -> str:
    """The TLS basic pair under `name`, its station trusting `station_trust`."""
    data: dict[str, Any] = json.loads(tls_bench())
    data["name"] = name
    for instance in data["instances"]:
        if instance["name"] == "server-1":
            instance["tls"]["trust"] = f"secret:{station_trust}"
    return json.dumps(data)


def runner_for(tmp_path: Path, secrets: Path) -> tuple[Runner, ProjectStore]:
    project = tmp_path / "project"
    project.mkdir()
    store = ProjectStore(project)
    engine = DockerEngine(
        image=identity("IGS_RUNTIME_IMAGE", "docker/runtime.Dockerfile"),
        relay=identity("IGS_RELAY_IMAGE", "docker/relay.Dockerfile"),
    )
    runner = Runner(
        store,
        RunStore(project),
        engine,
        secrets=SecretStore(secrets, project=project),
    )
    return runner, store


async def settle(runner: Runner, run_id: str) -> str:
    """The status of a run once it has ended."""
    async with asyncio.timeout(RUN_PATIENCE):
        while True:
            status = runner.get(run_id).status
            if status in TERMINAL:
                return str(status)
            await asyncio.sleep(POLL_SECONDS)


def journal_of(runner: Runner, run_id: str) -> list[dict[str, Any]]:
    return runner.events(run_id, limit=200, cursor=None).events


async def test_a_secured_bench_passes_the_acceptance_scenario(tmp_path: Path) -> None:
    runner, store = runner_for(tmp_path, secrets_in(tmp_path))
    service.create(store, "bench", "tls-pair", bench_named("tls-pair", "root"))
    service.create(store, "scenario", "tls-accepted", ACCEPTED)
    started = await runner.start("tls-accepted")
    status = await settle(runner, started.id)
    assert status == "passed", journal_of(runner, started.id)
    verdicts = [e for e in journal_of(runner, started.id) if e["kind"] == "verdict"]
    assert [v["outcome"] for v in verdicts] == ["passed"] * 3
    owner = Ownership(project=PROJECT_LABEL, bench="tls-pair", run=started.id)
    assert await DockerAdapter.connect().owned(owner) == [], "the bench was removed"


async def test_a_station_that_refuses_the_client_passes_the_rejection_scenario(
    tmp_path: Path,
) -> None:
    runner, store = runner_for(tmp_path, secrets_in(tmp_path))
    service.create(
        store, "bench", "tls-untrusted", bench_named("tls-untrusted", "other-root")
    )
    service.create(store, "scenario", "tls-refused", REFUSED)
    started = await runner.start("tls-refused")
    status = await settle(runner, started.id)
    journal = journal_of(runner, started.id)
    assert status == "passed", journal
    (verdict,) = [e for e in journal if e["kind"] == "verdict"]
    assert "refused the handshake: TLS_NO_CA_MATCH" in verdict["detail"], verdict
    owner = Ownership(project=PROJECT_LABEL, bench="tls-untrusted", run=started.id)
    assert await DockerAdapter.connect().owned(owner) == [], "the bench was removed"
