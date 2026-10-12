"""A bench whose connections are TLS, with its material delivered from the secrets
directory (IMPLEMENTATION.md §6, §9; task V1b).

The certificates are made for the test with the `openssl` command line, in a scratch
directory: a root, and a station and a client certificate it signs. Only the five DER
files the bench names are copied into the secrets directory, owner-only and outside the
project. The bench starts with that material, the client connects over TLS, and it
interrogates the station and reads its value. The images are named as for the other
Docker tests.
"""

import asyncio
import json
import os
import secrets as random
import subprocess
from pathlib import Path

import pytest
from docker.errors import NotFound

from docker import from_env  # type: ignore[attr-defined]
from igs_iec104_mcp.definitions.secrets import SecretStore
from igs_iec104_mcp.docker.adapter import DockerAdapter
from igs_iec104_mcp.docker.bench import start_bench
from igs_iec104_mcp.docker.labels import Ownership
from igs_iec104_mcp.models.loader import load_bench, load_document
from igs_iec104_mcp.runtime.api import ControlClient

ROOT = Path(__file__).resolve().parents[2]
BASIC_PAIR = ROOT / "examples/benches/basic-pair.yaml"
PROJECT = "igs-test"
ADDRESS = 100
VALUE = 12.5
PATIENCE_STEPS = 300
POLL_SECONDS = 0.1
# The secrets the bench names, and the DER file of the scratch PKI each one is.
SECRETS = {
    "station-cert": "station-cert.der",
    "station-key": "station-key.der",
    "client-cert": "client-cert.der",
    "client-key": "client-key.der",
    "root": "root.der",
}


def openssl(directory: Path, *args: str) -> None:
    result = subprocess.run(
        ["openssl", *args], cwd=directory, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"openssl {args[0]} failed: {result.stderr}"


def make_pki(directory: Path) -> None:
    """A root, and a station and a client certificate it signs, as DER files."""
    openssl(
        directory,
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "2",
        "-keyout",
        "root.key",
        "-out",
        "root.pem",
        "-subj",
        "/CN=test-root",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
    )
    for stem, name in (("station", "station.test"), ("client", "client.test")):
        openssl(
            directory,
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            f"{stem}.key",
            "-out",
            f"{stem}.csr",
            "-subj",
            f"/CN={name}",
        )
        (directory / f"{stem}.ext").write_text(
            f"subjectAltName=DNS:{name},IP:127.0.0.1\n"
            "extendedKeyUsage=serverAuth,clientAuth\n",
            encoding="utf-8",
        )
        openssl(
            directory,
            "x509",
            "-req",
            "-in",
            f"{stem}.csr",
            "-CA",
            "root.pem",
            "-CAkey",
            "root.key",
            "-CAcreateserial",
            "-days",
            "2",
            "-extfile",
            f"{stem}.ext",
            "-out",
            f"{stem}.pem",
        )
        openssl(
            directory,
            "x509",
            "-in",
            f"{stem}.pem",
            "-outform",
            "DER",
            "-out",
            f"{stem}-cert.der",
        )
        openssl(
            directory,
            "pkcs8",
            "-topk8",
            "-nocrypt",
            "-in",
            f"{stem}.key",
            "-outform",
            "DER",
            "-out",
            f"{stem}-key.der",
        )
    openssl(directory, "x509", "-in", "root.pem", "-outform", "DER", "-out", "root.der")


def write_secrets(secrets_dir: Path, scratch: Path) -> None:
    """The secrets directory: owner-only, with the five DER files as secrets."""
    make_pki(scratch)
    secrets_dir.mkdir(mode=0o700)
    secrets_dir.chmod(0o700)
    for name, source in SECRETS.items():
        target = secrets_dir / name
        target.write_bytes((scratch / source).read_bytes())
        target.chmod(0o600)


def tls_bench() -> str:
    data = load_document(BASIC_PAIR.read_text(encoding="utf-8"))
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
    return json.dumps(data, default=str)


async def served(client: ControlClient) -> bool:
    """Whether the client's image holds the station's value. The answer to an
    interrogation comes after its request, so the image is polled for a while."""
    for _ in range(PATIENCE_STEPS):
        points = (await client.image_points())["points"]
        if any(p["address"] == ADDRESS and p["value"] == VALUE for p in points):
            return True
        await asyncio.sleep(POLL_SECONDS)
    return False


def identity(variable: str, dockerfile: str) -> str:
    reference = os.environ.get(variable)
    message = f"set {variable} to the image built from {dockerfile}"
    if not reference:
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    try:
        value: str = from_env().images.get(reference).id
    except NotFound:
        pytest.fail(f"no image {reference!r} on the engine: {message}")
    return value


async def test_a_tls_bench_starts_with_its_material_and_serves_its_value(
    adapter: DockerAdapter, tmp_path: Path
) -> None:
    runtime = identity("IGS_RUNTIME_IMAGE", "docker/runtime.Dockerfile")
    relay = identity("IGS_RELAY_IMAGE", "docker/relay.Dockerfile")
    scratch = tmp_path / "pki"
    scratch.mkdir()
    secrets_dir = tmp_path / "secrets"
    write_secrets(secrets_dir, scratch)
    store = SecretStore(secrets_dir, project=tmp_path / "project")
    bench = load_bench(tls_bench())
    material = {
        "server-1": {
            "tls-certificate.der": store.read("secret:station-cert"),
            "tls-key.der": store.read("secret:station-key"),
            "tls-trust.der": store.read("secret:root"),
        },
        "client-1": {
            "tls-certificate.der": store.read("secret:client-cert"),
            "tls-key.der": store.read("secret:client-key"),
            "tls-trust.der": store.read("secret:root"),
        },
    }
    run = random.token_hex(16)
    running = await start_bench(
        adapter,
        bench,
        project=PROJECT,
        run=run,
        image=runtime,
        relay=relay,
        evidence=tmp_path / "evidence",
        ready_timeout=60.0,
        material=material,
    )
    try:
        client = running.client("client-1")
        ready = await client.ready()
        assert ready["connection"] == "up", (
            "the TLS handshake brought the connection up"
        )
        await client.start_transfer()
        await client.general_interrogation()
        assert await served(client), "the station's value reached the client"
    finally:
        report = await running.stop()
    assert report.graceful, report
    owner = Ownership(project=PROJECT, bench=bench.name, run=run)
    assert await adapter.owned(owner) == [], "the bench was removed"
