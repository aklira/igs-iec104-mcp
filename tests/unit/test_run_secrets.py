"""The secrets a run uses (IMPLEMENTATION.md §6, §10; task V1b).

A run reads the secrets its bench names before the bench starts, and hands the content
to the engine. The journal and the report record each secret's name and digest, never
its content. A bench whose secret is missing, or whose server has no secrets directory,
is refused before the run is created.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from test_scenario_runner import (
    BENCH,
    FakeEngine,
    FakeHandle,
    project,
    settled,
)

from igs_iec104_mcp.definitions.secrets import SecretStore, digest
from igs_iec104_mcp.definitions.service import ServiceError
from igs_iec104_mcp.models.loader import load_document
from igs_iec104_mcp.models.run import RunStatus
from igs_iec104_mcp.reporting.reports import report_of
from igs_iec104_mcp.scenarios.runner import Runner

STATION_CERT = b"STATION-CERTIFICATE-BYTES"
STATION_KEY = b"STATION-PRIVATE-KEY-BYTES"
ROOT = b"ROOT-CERTIFICATE-BYTES"
CLIENT_CERT = b"CLIENT-CERTIFICATE-BYTES"
CLIENT_KEY = b"CLIENT-PRIVATE-KEY-BYTES"
SECRETS = {
    "station-cert": STATION_CERT,
    "station-key": STATION_KEY,
    "root": ROOT,
    "client-cert": CLIENT_CERT,
    "client-key": CLIENT_KEY,
}


def secured_bench() -> str:
    data = load_document(BENCH)
    for instance in data["instances"]:
        if instance["name"] == "server-1":
            instance["tls"] = {
                "certificate": "secret:station-cert",
                "key": "secret:station-key",
                "trust": "secret:root",
            }
        if instance["name"] == "client-1":
            instance["tls"] = {
                "certificate": "secret:client-cert",
                "key": "secret:client-key",
                "trust": "secret:root",
                "server_name": "station.test",
            }
    return json.dumps(data)


class RecordingEngine(FakeEngine):
    """A fake engine that keeps the material it was given."""

    def __init__(self) -> None:
        super().__init__()
        self.material: dict[str, dict[str, bytes]] | None = None

    async def start(
        self,
        bench: Any,
        run_id: str,
        evidence: Path,
        material: Any,
        await_clients: bool,
    ) -> FakeHandle:
        self.material = material
        return await super().start(bench, run_id, evidence, material, await_clients)


def secrets_dir(tmp_path: Path, files: dict[str, bytes]) -> Path:
    directory = tmp_path.parent / f"{tmp_path.name}-secrets"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    for name, content in files.items():
        path = directory / name
        path.write_bytes(content)
        path.chmod(0o600)
    return directory


async def test_a_run_gives_the_engine_the_content_and_records_only_digests(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path, secured_bench())
    secrets = SecretStore(secrets_dir(tmp_path, SECRETS), project=tmp_path)
    engine = RecordingEngine()
    runner = Runner(store, runs, engine, secrets=secrets)
    started = await runner.start("interrogate")
    record = await settled(runner, started.id)
    assert record.status is RunStatus.PASSED

    assert engine.material is not None
    assert engine.material["server-1"]["tls-certificate.der"] == STATION_CERT
    assert engine.material["client-1"]["tls-key.der"] == CLIENT_KEY

    events = runs.events(started.id, limit=100).events
    recorded = next(e for e in events if e["kind"] == "secrets")
    items = {(i["instance"], i["file"]): i for i in recorded["items"]}
    assert items[("server-1", "tls-key.der")]["sha256"] == digest(STATION_KEY)
    assert items[("server-1", "tls-key.der")]["reference"] == "secret:station-key"

    journal = (tmp_path / "runs" / started.id / "events.jsonl").read_text(
        encoding="utf-8"
    )
    report = report_of(runs, started.id, "json").content
    for secret in SECRETS.values():
        assert secret.decode() not in journal
        assert secret.decode() not in report
    document = json.loads(report)
    assert {i["sha256"] for i in document["inputs"]["secrets"]} == {
        digest(content) for content in SECRETS.values()
    }


async def test_a_bench_that_names_secrets_without_a_directory_is_refused(
    tmp_path: Path,
) -> None:
    store, runs = project(tmp_path, secured_bench())
    engine = RecordingEngine()
    runner = Runner(store, runs, engine)
    with pytest.raises(ServiceError) as error:
        await runner.start("interrogate")
    assert error.value.code == "invalid_definition"
    assert "--secrets-dir" in str(error.value)
    assert runs.list_runs(limit=10).items == []
    assert engine.material is None


async def test_a_missing_secret_is_named_and_no_run_is_created(tmp_path: Path) -> None:
    store, runs = project(tmp_path, secured_bench())
    files = {name: content for name, content in SECRETS.items() if name != "root"}
    secrets = SecretStore(secrets_dir(tmp_path, files), project=tmp_path)
    engine = RecordingEngine()
    runner = Runner(store, runs, engine, secrets=secrets)
    with pytest.raises(ServiceError) as error:
        await runner.start("interrogate")
    assert error.value.code == "invalid_definition"
    assert "secret:root is not a file" in str(error.value)
    assert runs.list_runs(limit=10).items == []
    assert engine.material is None


async def test_a_plain_bench_takes_no_secrets(tmp_path: Path) -> None:
    store, runs = project(tmp_path)
    engine = RecordingEngine()
    runner = Runner(store, runs, engine, secrets=None)
    started = await runner.start("interrogate")
    await settled(runner, started.id)
    assert engine.material == {}
    assert "secrets" not in [
        e["kind"] for e in runs.events(started.id, limit=100).events
    ]
