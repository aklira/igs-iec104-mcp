"""The registration of the server with an MCP client (IMPLEMENTATION.md §14, task I6;
issue #1; D-061).

`scripts/install.sh --client claude|codex` calls this module once the images are built.
It registers the server under one name, with the client's own command: the user scope
for Claude Code (`--scope user`), and the global configuration for Codex. It never edits
a client's configuration file. It reads the entry that exists first, and writes only
when the entry is missing, or when it is different and `--force` is given. It shows the
exact commands and asks before it runs them, unless `--yes` is given.

Both clients print environment values in clear, so this module prints the names of the
fields that differ, never their values.
"""

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

NAME = "igs-iec104"
CLIENTS = ("claude", "codex")
TIMEOUT = 60.0


class Found(StrEnum):
    ABSENT = "absent"
    SAME = "same"
    DIFFERENT = "different"


@dataclass(frozen=True)
class Spec:
    """The registration the installer wants: a command, its arguments, and its env."""

    name: str
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str]


@dataclass(frozen=True)
class Inspection:
    found: Found
    differences: tuple[str, ...] = ()


@dataclass(frozen=True)
class Output:
    """What a client CLI printed. Its data goes to stdout and its diagnostics to
    stderr: the Codex CLI prints a warning on stderr next to its JSON."""

    code: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str]], Output]
Asker = Callable[[str], str]


def desired_spec(
    repository: Path,
    project_dir: Path,
    runtime_image: str,
    relay_image: str,
    docker_host: str,
) -> Spec:
    environment = {
        "PYTHONPATH": str(repository / "src"),
        "DOCKER_HOST": docker_host,
    }
    args = (
        "-m",
        "igs_iec104_mcp",
        "--project",
        str(project_dir),
        "--runtime-image",
        runtime_image,
        "--relay-image",
        relay_image,
    )
    return Spec(NAME, str(repository / ".venv" / "bin" / "python"), args, environment)


def _env_flags(flag: str, env: Mapping[str, str]) -> list[str]:
    return [part for key, value in env.items() for part in (flag, f"{key}={value}")]


def add_command(client: str, spec: Spec) -> list[str]:
    if client == "claude":
        return [
            "claude",
            "mcp",
            "add",
            "--scope",
            "user",
            spec.name,
            *_env_flags("-e", spec.env),
            "--",
            spec.command,
            *spec.args,
        ]
    return [
        "codex",
        "mcp",
        "add",
        spec.name,
        *_env_flags("--env", spec.env),
        "--",
        spec.command,
        *spec.args,
    ]


def remove_command(client: str, name: str) -> list[str]:
    if client == "claude":
        return ["claude", "mcp", "remove", "--scope", "user", name]
    return ["codex", "mcp", "remove", name]


def _claude_fields(text: str) -> tuple[str, str, dict[str, str]]:
    """The command, the joined arguments, and the environment of `claude mcp get`."""
    command = arguments = ""
    environment: dict[str, str] = {}
    in_environment = False
    for line in text.splitlines():
        stripped = line.strip()
        if line.startswith("  Command:"):
            command, in_environment = stripped.removeprefix("Command:").strip(), False
        elif line.startswith("  Args:"):
            arguments, in_environment = stripped.removeprefix("Args:").strip(), False
        elif line.startswith("  Environment:"):
            in_environment = True
        elif in_environment and line.startswith("    ") and "=" in stripped:
            key, _, value = stripped.partition("=")
            environment[key] = value
        elif not line.strip():
            in_environment = False
    return command, arguments, environment


def _differences(
    spec: Spec, command: str, arguments_match: bool, env: Mapping[str, str]
) -> tuple[str, ...]:
    """The fields that differ, by name. Values of the environment are never named."""
    differences = []
    if command != spec.command:
        differences.append("command")
    if not arguments_match:
        differences.append("arguments")
    if dict(env) != dict(spec.env):
        names = set(env) ^ set(spec.env)
        differences.append(
            "environment (" + (", ".join(sorted(names)) or "values") + ")"
        )
    return tuple(differences)


def inspect(client: str, spec: Spec, run: Runner) -> Inspection:
    if client == "claude":
        result = run(["claude", "mcp", "get", spec.name])
        if result.code != 0:
            return Inspection(Found.ABSENT)
        command, arguments, env = _claude_fields(result.stdout)
        # Claude prints the arguments joined by spaces: compare that text.
        differences = _differences(spec, command, arguments == " ".join(spec.args), env)
    else:
        result = run(["codex", "mcp", "get", spec.name, "--json"])
        if result.code != 0:
            return Inspection(Found.ABSENT)
        transport = json.loads(result.stdout)["transport"]
        differences = _differences(
            spec,
            transport["command"],
            tuple(transport["args"]) == spec.args,
            transport.get("env") or {},
        )
    if differences:
        return Inspection(Found.DIFFERENT, differences)
    return Inspection(Found.SAME)


def _run_command(argv: Sequence[str]) -> Output:
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return Output(127, "", str(error))
    return Output(done.returncode, done.stdout, done.stderr)


def register(
    client: str,
    spec: Spec,
    *,
    run: Runner,
    which: Callable[[str], str | None],
    out: Callable[[str], None],
    force: bool = False,
    yes: bool = False,
    ask: Asker | None = None,
) -> int:
    """Registers the server with the client. Returns 0 when the entry is as specified
    afterwards, and 1 when it is not, with the reason printed."""
    if which(client) is None:
        out(f"{client} is not on PATH: install it, then run the installer again")
        return 1
    current = inspect(client, spec, run)
    if current.found is Found.SAME:
        out(f"{client}: {spec.name} is registered as specified; nothing changes")
        return 0
    if current.found is Found.DIFFERENT and not force:
        out(
            f"{client}: {spec.name} is registered differently "
            f"({', '.join(current.differences)}); nothing changes. Run again with "
            "--force to replace it"
        )
        return 1

    commands: list[list[str]] = []
    if current.found is Found.DIFFERENT:
        commands.append(remove_command(client, spec.name))
    commands.append(add_command(client, spec))
    out("This will run:")
    for command in commands:
        out("  " + shlex.join(command))
    if not yes:
        if ask is None:
            out("not a terminal: run again with --yes to confirm")
            return 1
        if ask("Run these commands? [y/N] ").strip().lower() not in ("y", "yes"):
            out(f"{client}: nothing changed")
            return 1
    for command in commands:
        result = run(command)
        if result.code != 0:
            details = (result.stderr or result.stdout).strip()
            out(f"{client}: the command failed: {shlex.join(command)}\n{details}")
            return 1
    if inspect(client, spec, run).found is not Found.SAME:
        out(f"{client}: the registration does not match after the change")
        return 1
    out(f"{client}: {spec.name} is registered")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    run: Runner = _run_command,
    which: Callable[[str], str | None] = shutil.which,
    ask: Asker | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m igs_iec104_mcp.registration",
        description="Registers the server with an MCP client, after confirmation.",
    )
    parser.add_argument("--client", choices=CLIENTS, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--relay-image", required=True)
    parser.add_argument("--docker-host", required=True)
    parser.add_argument(
        "--force", action="store_true", help="replace an entry that differs"
    )
    parser.add_argument(
        "--yes", action="store_true", help="do not ask, for a new entry"
    )
    args = parser.parse_args(argv)
    spec = desired_spec(
        args.repository,
        args.project_dir,
        args.runtime_image,
        args.relay_image,
        args.docker_host,
    )
    if ask is None and sys.stdin.isatty():
        ask = input
    return register(
        args.client,
        spec,
        run=run,
        which=which,
        out=print,
        force=args.force,
        yes=args.yes,
        ask=ask,
    )


if __name__ == "__main__":
    sys.exit(main())
