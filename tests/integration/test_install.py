"""The installer, run as a person runs it (IMPLEMENTATION.md §14, task I4; issue #1).

The script runs the locked environment and the preflight for real. The container engine
is a socket that answers as Podman, and the engine CLI is a stub: it records each build
and gives each image a stable identity, so no image is built here. The uv stub stops
`uv sync` from changing the environment, and forwards every other call to the real uv.
"""

import hashlib
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "install.sh"
RUNTIME_TAG = "igs-iec104-runtime:local"
RELAY_TAG = "igs-iec104-relay:local"
IDENTITY = re.compile(r"^sha256:[0-9a-f]{64}$")

ENGINE_STUB = r"""#!/usr/bin/env bash
set -eu
echo "$*" >>"$IGS_TEST_ENGINE_LOG"
case "$1" in
    build) ;;
    image) printf '%s\n' "$(printf '%s' "${!#}" | sha256sum | cut -d' ' -f1)" ;;
    *) echo "engine stub: unsupported $*" >&2; exit 1 ;;
esac
"""

UV_STUB = r"""#!/usr/bin/env bash
echo "$*" >>"$IGS_TEST_UV_LOG"
if [ "$1" = sync ]; then
    exit 0
fi
exec "$IGS_TEST_REAL_UV" "$@"
"""


# A client CLI with the behaviour of the real ones, as the probes of Q-004 recorded it:
# Claude refuses a duplicate name (exit 0), Codex overwrites it, and `get` exits 1 when
# the entry is absent. The entries are kept in a JSON file per client.
CLIENT_STUB = r"""#!__PYTHON__
import json
import os
import sys
from pathlib import Path

client = Path(sys.argv[0]).name
store_dir = Path(os.environ["IGS_TEST_CLIENT_STORE"])
store = store_dir / f"{client}.json"
entries = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
args = sys.argv[1:]
verb = args[1]


def save() -> None:
    store_dir.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(entries), encoding="utf-8")


def parse_add(tokens):
    env = {}
    name = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            entry = {"command": tokens[index + 1], "args": tokens[index + 2 :]}
            return name, {**entry, "env": env}
        if token == "--scope":
            index += 2
            continue
        if token in ("-e", "--env"):
            key, _, value = tokens[index + 1].partition("=")
            env[key] = value
            index += 2
            continue
        name = token
        index += 1
    raise SystemExit(2)


if verb == "add":
    name, entry = parse_add(args[2:])
    if client == "claude" and name in entries:
        print(f"MCP server {name} already exists in user config")
        raise SystemExit(0)
    entries[name] = entry
    save()
    print(f"Added {name}")
elif verb == "get":
    name = args[2]
    if name not in entries:
        print(f"No MCP server found with name: {name}", file=sys.stderr)
        raise SystemExit(1)
    entry = entries[name]
    if client == "claude":
        lines = [
            f"{name}:",
            "  Scope: User config (available in all your projects)",
            "  Type: stdio",
            f"  Command: {entry['command']}",
        ]
        if entry["args"]:
            lines.append("  Args: " + " ".join(entry["args"]))
        if entry["env"]:
            lines.append("  Environment:")
            lines += [f"    {key}={value}" for key, value in entry["env"].items()]
        print("\n".join(lines))
    else:
        transport = {
            "type": "stdio",
            "command": entry["command"],
            "args": entry["args"],
            "env": entry["env"] or None,
        }
        print(json.dumps({"name": name, "transport": transport}))
elif verb == "remove":
    name = args[-1]
    if name not in entries:
        print(f"No MCP server named {name}", file=sys.stderr)
        raise SystemExit(1)
    del entries[name]
    save()
    print(f"Removed {name}")
else:
    raise SystemExit(f"client stub: unsupported {verb}")
"""


class _Engine(socketserver.ThreadingUnixStreamServer):
    def get_request(self) -> tuple[socket.socket, Any]:
        request, _ = super().get_request()
        return request, ("local", 0)


class _Podman(http.server.BaseHTTPRequestHandler):
    """Answers the Docker-compatible API as Podman does: the version names Podman."""

    def do_GET(self) -> None:
        self._answer(with_body=True)

    def do_HEAD(self) -> None:
        self._answer(with_body=False)

    def _answer(self, *, with_body: bool) -> None:
        if self.path.endswith("/version"):
            body = json.dumps(
                {
                    "ApiVersion": "1.41",
                    "Version": "5.7.0",
                    "Components": [{"Name": "Podman Engine"}],
                }
            ).encode()
        else:
            body = b"OK"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@dataclass(frozen=True)
class Harness:
    env: dict[str, str]
    project: Path
    engine_log: Path
    uv_log: Path
    state: Path
    clients: Path

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )


def _require_prerequisites() -> None:
    """The preflight needs the runtime's build prerequisites, as the doctor reports
    them. Missing ones skip the test, except in CI, where they fail it."""
    missing = []
    if shutil.which("uv") is None:
        missing.append("uv")
    if shutil.which("pkg-config") is None or (
        subprocess.run(["pkg-config", "--exists", "openssl"], check=False).returncode
        != 0
    ):
        missing.append("pkg-config and the OpenSSL development files")
    if missing:
        message = f"the installer test needs: {', '.join(missing)}"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    _require_prerequisites()
    real_uv = shutil.which("uv")
    assert real_uv is not None
    # A socket path is at most 108 bytes, so the sockets live in a short directory.
    short = Path(tempfile.mkdtemp(prefix="igs-install-", dir="/tmp"))
    bin_dir = short / "bin"
    bin_dir.mkdir()
    stubs = {
        "podman": ENGINE_STUB,
        "uv": UV_STUB,
        "claude": CLIENT_STUB.replace("__PYTHON__", sys.executable),
        "codex": CLIENT_STUB.replace("__PYTHON__", sys.executable),
    }
    for name, text in stubs.items():
        stub = bin_dir / name
        stub.write_text(text, encoding="utf-8")
        stub.chmod(0o755)
    socket_path = short / "podman.sock"
    server = _Engine(str(socket_path), _Podman)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    engine_log = tmp_path / "engine.log"
    uv_log = tmp_path / "uv.log"
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "IGS_TEST_REAL_UV": real_uv,
            "IGS_TEST_ENGINE_LOG": str(engine_log),
            "IGS_TEST_UV_LOG": str(uv_log),
            "DOCKER_HOST": f"unix://{socket_path}",
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "IGS_TEST_CLIENT_STORE": str(tmp_path / "clients"),
        }
    )
    try:
        yield Harness(
            env=env,
            project=tmp_path / "igs-project",
            engine_log=engine_log,
            uv_log=uv_log,
            state=tmp_path / "state" / "igs-iec104-mcp" / "install.json",
            clients=tmp_path / "clients",
        )
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(short, ignore_errors=True)


def _log_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_a_dry_run_prints_the_steps_and_changes_nothing(harness: Harness) -> None:
    done = harness.run("--dry-run", "--project-dir", str(harness.project))
    assert done.returncode == 0, done.stderr
    assert "dry run: nothing is changed" in done.stdout
    assert not harness.project.exists()
    assert not harness.state.exists()
    assert _log_lines(harness.engine_log) == []
    assert _log_lines(harness.uv_log) == []


def test_a_run_builds_both_images_and_names_them_by_identity(
    harness: Harness,
) -> None:
    done = harness.run("--project-dir", str(harness.project))
    assert done.returncode == 0, done.stderr + done.stdout
    assert harness.project.is_dir()

    builds = _log_lines(harness.engine_log)
    assert builds == [
        f"build --file docker/runtime.Dockerfile --tag {RUNTIME_TAG} runtime",
        f"build --file docker/relay.Dockerfile --tag {RELAY_TAG} docker",
        f"image inspect --format {{{{.Id}}}} {RUNTIME_TAG}",
        f"image inspect --format {{{{.Id}}}} {RELAY_TAG}",
    ]
    assert "sync --locked" in _log_lines(harness.uv_log)

    state = json.loads(harness.state.read_text(encoding="utf-8"))
    assert state["engine"] == "podman"
    assert state["schema"] == 1
    assert state["project_dir"] == str(harness.project)
    assert Path(state["repository"]).is_absolute()
    # Podman reports the identity without its prefix; the state holds it with one.
    for key, tag in (("runtime_image", RUNTIME_TAG), ("relay_image", RELAY_TAG)):
        expected = "sha256:" + hashlib.sha256(tag.encode()).hexdigest()
        assert state[key] == expected
        assert IDENTITY.match(state[key])
    assert RUNTIME_TAG not in harness.state.read_text(encoding="utf-8")
    assert RELAY_TAG not in harness.state.read_text(encoding="utf-8")


def test_a_second_run_changes_no_state(harness: Harness) -> None:
    first = harness.run("--project-dir", str(harness.project))
    assert first.returncode == 0, first.stderr + first.stdout
    before = harness.state.read_bytes()
    second = harness.run("--project-dir", str(harness.project))
    assert second.returncode == 0, second.stderr + second.stdout
    assert harness.state.read_bytes() == before
    assert "state unchanged" in second.stdout


def test_an_engine_that_is_not_the_one_asked_for_stops_before_any_build(
    harness: Harness,
) -> None:
    done = harness.run("--engine", "docker", "--project-dir", str(harness.project))
    assert done.returncode == 1
    assert "the preflight failed" in done.stderr
    assert "answers as podman, not docker" in done.stdout
    assert _log_lines(harness.engine_log) == []
    assert not harness.state.exists()


def test_an_unknown_option_is_a_usage_error(harness: Harness) -> None:
    done = harness.run("--surprise")
    assert done.returncode == 2
    assert "unknown option" in done.stderr


def _registered(harness: Harness, client: str) -> dict[str, Any]:
    path = harness.clients / f"{client}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_a_new_registration_is_added_once_and_then_left_alone(harness: Harness) -> None:
    first = harness.run(
        "--client", "claude", "--yes", "--project-dir", str(harness.project)
    )
    assert first.returncode == 0, first.stdout + first.stderr
    entry = _registered(harness, "claude")["igs-iec104"]
    assert entry["command"] == str(ROOT / ".venv" / "bin" / "python")
    assert entry["env"]["PYTHONPATH"] == str(ROOT / "src")
    assert entry["env"]["DOCKER_HOST"] == harness.env["DOCKER_HOST"]
    assert "--runtime-image" in entry["args"]
    before = (harness.clients / "claude.json").read_bytes()
    second = harness.run(
        "--client", "claude", "--yes", "--project-dir", str(harness.project)
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert "nothing changes" in second.stdout
    assert (harness.clients / "claude.json").read_bytes() == before


def test_a_different_registration_is_kept_without_force_and_replaced_with_it(
    harness: Harness,
) -> None:
    harness.clients.mkdir(parents=True)
    other = {"command": "/old/python", "args": [], "env": {}}
    (harness.clients / "codex.json").write_text(
        json.dumps({"igs-iec104": other}), encoding="utf-8"
    )
    refused = harness.run(
        "--client", "codex", "--yes", "--project-dir", str(harness.project)
    )
    assert refused.returncode == 1
    assert "registered differently" in refused.stdout
    assert _registered(harness, "codex") == {"igs-iec104": other}
    replaced = harness.run(
        "--client", "codex", "--force", "--yes", "--project-dir", str(harness.project)
    )
    assert replaced.returncode == 0, replaced.stdout + replaced.stderr
    entry = _registered(harness, "codex")["igs-iec104"]
    assert entry["command"] == str(ROOT / ".venv" / "bin" / "python")


def test_a_dry_run_with_a_client_registers_nothing(harness: Harness) -> None:
    done = harness.run(
        "--dry-run", "--client", "claude", "--project-dir", str(harness.project)
    )
    assert done.returncode == 0, done.stderr
    assert "register the server with claude" in done.stdout
    assert not (harness.clients / "claude.json").exists()


def test_the_generic_client_mode_is_refused_for_now(harness: Harness) -> None:
    done = harness.run("--client", "generic", "--project-dir", str(harness.project))
    assert done.returncode == 2
    assert "not implemented yet" in done.stderr
