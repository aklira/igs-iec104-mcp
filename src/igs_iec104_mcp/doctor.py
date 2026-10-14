"""The read-only preflight of the installation (IMPLEMENTATION.md §14, task I2).

It reports the prerequisites of the server and of its benches without changing the
system: no package is installed, no group is changed, no image is built, and no file
is written by the checks. A check that fails names the corrective step, which the user
or the installer runs (D-056). Run it with `python -m igs_iec104_mcp.doctor`; `--json`
prints the result for a program. It exits with 1 when a required check fails, and 0
otherwise.

Two things it may create, both removed by the doctor: a temporary project for the
server's handshake (igs_iec104_mcp.handshake), and, with `--run-bench` only, a bench
that the example scenario starts on the engine, which the run's cleanup removes
(igs_iec104_mcp.bench_run). The handshake validates the example bench, and the run
starts containers; neither uses the project directory.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from docker.errors import DockerException

# The SDK ships no type hints (see pyproject.toml): its client is typed Any here.
from docker import DockerClient  # type: ignore[attr-defined]
from igs_iec104_mcp.bench_run import bench_checks
from igs_iec104_mcp.checks import Check, Report, Status
from igs_iec104_mcp.handshake import run_server_checks

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_TOOLCHAIN = ROOT / "runtime" / "rust-toolchain.toml"
OSRELEASE = Path("/proc/sys/kernel/osrelease")
DOCKER_SOCKET = "unix:///var/run/docker.sock"
ENGINES = ("auto", "podman", "docker")
COMMAND_TIMEOUT = 10.0
ENGINE_TIMEOUT = 5.0


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str


@dataclass(frozen=True)
class EngineReply:
    kind: str
    version: str


class EngineUnreachable(Exception):
    """The engine at a socket did not answer."""


class Probe(Protocol):
    """What the checks may observe. The system probe reads the machine; a test probe
    answers from a table."""

    def which(self, program: str) -> str | None: ...

    def run(self, argv: Sequence[str]) -> CommandResult | None: ...

    def read_text(self, path: Path) -> str | None: ...

    def engine_version(self, host: str) -> EngineReply:
        """Raises EngineUnreachable when the engine does not answer."""
        ...


class SystemProbe:
    """Reads the machine. Commands run without a shell, with a timeout, and only read
    state."""

    def which(self, program: str) -> str | None:
        return shutil.which(program)

    def run(self, argv: Sequence[str]) -> CommandResult | None:
        try:
            done = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return CommandResult(done.returncode, done.stdout.strip())

    def read_text(self, path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def engine_version(self, host: str) -> EngineReply:
        socket = Path(host.removeprefix("unix://"))
        if host.startswith("unix://") and not socket.exists():
            raise EngineUnreachable(f"no socket at {socket}")
        try:
            client = DockerClient(base_url=host, timeout=ENGINE_TIMEOUT)
            try:
                info: Mapping[str, Any] = client.version()
            finally:
                client.close()
        except (DockerException, OSError) as error:
            raise EngineUnreachable(str(error)) from error
        return EngineReply(
            kind=engine_kind(info.get("Components") or []),
            version=str(info.get("Version", "unknown")),
        )


def engine_kind(components: Sequence[Mapping[str, Any]]) -> str:
    """Podman's compatible API names "Podman Engine" among its components; Docker
    Engine names "Engine"."""
    names = [str(component.get("Name", "")) for component in components]
    return "podman" if any("Podman" in name for name in names) else "docker"


def _podman_host(env: Mapping[str, str], uid: int) -> str:
    runtime_dir = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    return f"unix://{runtime_dir}/podman/podman.sock"


def check_host(platform: str, osrelease: str | None) -> Check:
    if platform != "linux":
        return Check(
            "host",
            Status.FAIL,
            f"{platform} is not a supported host",
            "run the server on Linux, or on Windows inside WSL2 (docs/setup.md)",
        )
    kernel = (osrelease or "").strip() or "unknown kernel"
    if "microsoft" in kernel.lower():
        if "wsl2" in kernel.lower():
            return Check("host", Status.OK, f"WSL2 ({kernel})", data={"host": "wsl2"})
        return Check(
            "host",
            Status.WARNING,
            f"WSL 1 ({kernel}) is not tested",
            "use WSL2, which the platform matrix tests",
            data={"host": "wsl1"},
        )
    return Check("host", Status.OK, f"Linux ({kernel})", data={"host": "linux"})


def check_python(version: tuple[int, int]) -> Check:
    found = f"{version[0]}.{version[1]}"
    if version < (3, 12):
        return Check(
            "python",
            Status.FAIL,
            f"Python {found}; 3.12 is required",
            "install Python 3.12 (.python-version), then run uv sync --locked",
        )
    return Check("python", Status.OK, f"Python {found}")


def check_uv(probe: Probe) -> Check:
    path = probe.which("uv")
    if path is None:
        return Check(
            "uv",
            Status.FAIL,
            "uv not found",
            "install uv (https://docs.astral.sh/uv/), then run uv sync --locked",
        )
    result = probe.run([path, "--version"])
    detail = result.stdout if result and result.returncode == 0 else path
    return Check("uv", Status.OK, detail)


def _pinned_rust(text: str | None) -> str | None:
    if text is None:
        return None
    try:
        channel = tomllib.loads(text)["toolchain"]["channel"]
    except (tomllib.TOMLDecodeError, KeyError, TypeError):
        return None
    return channel if isinstance(channel, str) else None


def check_rust(probe: Probe, toolchain: Path) -> Check:
    pinned = _pinned_rust(probe.read_text(toolchain))
    rustc = probe.which("rustc")
    if rustc is None:
        return Check(
            "rust",
            Status.FAIL,
            "rustc not found",
            f"install Rust {pinned or 'the pinned version'} with rustup "
            "(https://rustup.rs)",
        )
    result = probe.run([rustc, "--version"])
    if result is None or result.returncode != 0:
        return Check(
            "rust",
            Status.FAIL,
            "rustc does not run",
            "check the Rust installation with rustup show, then run the doctor again",
        )
    words = result.stdout.split()
    found = words[1] if len(words) > 1 else result.stdout
    if pinned is None:
        return Check(
            "rust",
            Status.WARNING,
            f"rustc {found}; the pinned version is unknown",
            "run the doctor from the source tree",
        )
    if found != pinned:
        return Check(
            "rust",
            Status.WARNING,
            f"rustc {found}; the runtime pins {pinned}",
            f"run rustup toolchain install {pinned}",
        )
    return Check("rust", Status.OK, f"rustc {found}")


def check_openssl(probe: Probe) -> Check:
    pkg_config = probe.which("pkg-config")
    action = (
        "install pkg-config and the OpenSSL development files (Debian or Ubuntu: "
        "sudo apt install pkg-config libssl-dev)"
    )
    if pkg_config is None:
        return Check("openssl", Status.FAIL, "pkg-config not found", action)
    result = probe.run([pkg_config, "--modversion", "openssl"])
    if result is None or result.returncode != 0:
        return Check(
            "openssl", Status.FAIL, "OpenSSL development files not found", action
        )
    return Check("openssl", Status.OK, f"OpenSSL {result.stdout}")


def check_engine(
    probe: Probe, requested: str, env: Mapping[str, str], uid: int
) -> Check:
    host = env.get("DOCKER_HOST", "").strip()
    if host:
        candidates = [("DOCKER_HOST", host)]
    else:
        candidates = [
            (kind, url)
            for kind, url in (
                ("podman", _podman_host(env, uid)),
                ("docker", DOCKER_SOCKET),
            )
            if requested in ("auto", kind)
        ]
    errors: list[str] = []
    for label, url in candidates:
        try:
            reply = probe.engine_version(url)
        except EngineUnreachable as error:
            errors.append(f"{label} at {url}: {error}")
            continue
        if requested not in ("auto", reply.kind):
            return Check(
                "engine",
                Status.FAIL,
                f"{url} answers as {reply.kind}, not {requested}",
                f"point DOCKER_HOST at a {requested} socket, "
                f"or run with --engine {reply.kind}",
            )
        data = {
            "engine": reply.kind,
            "version": reply.version,
            "host": url,
            "uid": str(uid),
        }
        detail = f"{reply.kind} {reply.version} at {url}"
        if reply.kind == "podman" and uid == 0:
            return Check(
                "engine",
                Status.WARNING,
                f"{detail}, as root",
                "run Podman as your own user: the platform matrix tests rootless "
                "Podman",
                data,
            )
        return Check("engine", Status.OK, detail, data=data)
    if any("permission denied" in error.lower() for error in errors):
        action = (
            "your user may not use the socket: ask an administrator to grant "
            "access, or start your own Podman service. The doctor does not change "
            "groups"
        )
    else:
        action = (
            "start the Podman service (docs/setup.md#podman), or install Docker "
            "Engine; set DOCKER_HOST if the socket is elsewhere"
        )
    return Check(
        "engine",
        Status.FAIL,
        "no container engine answers: " + "; ".join(errors),
        action,
    )


def check_client(probe: Probe, name: str) -> Check:
    path = probe.which(name)
    if path is None:
        return Check(
            f"client-{name}",
            Status.WARNING,
            f"{name} not found",
            f"install {name} to register the server with it; the server runs "
            "without it",
        )
    return Check(f"client-{name}", Status.OK, f"{name} at {path}")


def check_project_dir(path: Path) -> Check:
    if path.exists():
        if not path.is_dir():
            return Check(
                "project-dir", Status.FAIL, f"{path} is not a directory", "choose one"
            )
        if not os.access(path, os.W_OK | os.X_OK):
            return Check(
                "project-dir",
                Status.FAIL,
                f"{path} is not writable by you",
                "choose a directory you own",
            )
        if path.stat().st_mode & 0o002:
            return Check(
                "project-dir",
                Status.WARNING,
                f"{path} is writable by everyone",
                "restrict it to your user: chmod o-w",
            )
        return Check("project-dir", Status.OK, f"{path} exists and is writable")
    parent = next(candidate for candidate in path.parents if candidate.exists())
    if not os.access(parent, os.W_OK | os.X_OK):
        return Check(
            "project-dir",
            Status.FAIL,
            f"{path} does not exist and {parent} is not writable by you",
            "choose a directory you own",
        )
    return Check(
        "project-dir",
        Status.OK,
        f"{path} does not exist yet; the installer creates it",
    )


def run_checks(
    probe: Probe,
    *,
    engine: str = "auto",
    project_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    uid: int | None = None,
    platform: str | None = None,
    python_version: tuple[int, int] | None = None,
    toolchain: Path = RUNTIME_TOOLCHAIN,
) -> Report:
    """Runs the checks in order. The arguments default to this machine."""
    environment = os.environ if env is None else env
    current_uid = os.getuid() if uid is None else uid
    checks = [
        check_host(
            sys.platform if platform is None else platform,
            probe.read_text(OSRELEASE),
        ),
        check_python(
            sys.version_info[:2] if python_version is None else python_version
        ),
        check_uv(probe),
        check_rust(probe, toolchain),
        check_openssl(probe),
        check_engine(probe, engine, environment, current_uid),
        check_client(probe, "claude"),
        check_client(probe, "codex"),
    ]
    if project_dir is not None:
        checks.append(check_project_dir(project_dir))
    return Report(tuple(checks))


_LABELS = {Status.OK: "ok", Status.WARNING: "warn", Status.FAIL: "FAIL"}


def format_text(report: Report) -> str:
    lines = ["igs-iec104-mcp preflight"]
    for check in report.checks:
        lines.append(f"  {_LABELS[check.status]:<5} {check.name:<14} {check.detail}")
        if check.action:
            lines.append(f"  {'':<5} {'':<14} -> {check.action}")
    failed = sum(check.status is Status.FAIL for check in report.checks)
    warned = sum(check.status is Status.WARNING for check in report.checks)
    if failed:
        lines.append(f"{failed} required check(s) failed.")
    else:
        lines.append("All required checks passed.")
    if warned:
        lines.append(f"{warned} warning(s).")
    return "\n".join(lines)


ServerChecks = Callable[[], Sequence[Check]]
BenchChecks = Callable[[Check | None, str | None, str | None], Sequence[Check]]


def main(
    argv: Sequence[str] | None = None,
    probe: Probe | None = None,
    server: ServerChecks | None = None,
    bench: BenchChecks | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m igs_iec104_mcp.doctor",
        description="Read-only preflight of the prerequisites. It changes nothing.",
    )
    parser.add_argument(
        "--engine",
        choices=ENGINES,
        default="auto",
        help="the container engine to check: Podman first, then Docker, when auto",
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        help="the project directory the installer will use: checked, not created",
    )
    parser.add_argument(
        "--server-only",
        action="store_true",
        help="check only the MCP server: the host checks are skipped, and the "
        "handshake and bench_validate run as usual",
    )
    parser.add_argument(
        "--run-bench",
        action="store_true",
        help="also run the example scenario on a managed bench, on the engine found: "
        "it starts containers, and its cleanup removes them",
    )
    parser.add_argument(
        "--runtime-image",
        help="the runtime image of the run, by identity (default: scripts/install.sh)",
    )
    parser.add_argument(
        "--relay-image",
        help="the relay image of the run, by identity (default: scripts/install.sh)",
    )
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    if args.run_bench and args.server_only:
        parser.error("--run-bench needs the engine check: drop --server-only")
    checks: list[Check] = []
    engine: Check | None = None
    if not args.server_only:
        report = run_checks(
            probe or SystemProbe(), engine=args.engine, project_dir=args.project_dir
        )
        checks.extend(report.checks)
        engine = next(
            (check for check in report.checks if check.name == "engine"), None
        )
    checks.extend((run_server_checks if server is None else server)())
    if args.run_bench:
        run = bench_checks if bench is None else bench
        checks.extend(run(engine, args.runtime_image, args.relay_image))
    report = Report(tuple(checks))
    if args.json:
        print(json.dumps(report.as_json(), indent=2))
    else:
        print(format_text(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
