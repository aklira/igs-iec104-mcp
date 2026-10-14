"""The registration of the server with an MCP client, against a client that keeps its
entry between calls (IMPLEMENTATION.md §14, task I6; D-061)."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from igs_iec104_mcp.registration import (
    NAME,
    Output,
    Spec,
    add_command,
    desired_spec,
    generic_entry,
    generic_text,
    main,
    register,
    remove_command,
)

SPEC = desired_spec(
    Path("/repo"),
    Path("/proj"),
    "sha256:aa",
    "sha256:bb",
    "unix:///run/user/1000/podman/podman.sock",
)


def _entry_from(spec: Spec) -> dict[str, Any]:
    return {"command": spec.command, "args": list(spec.args), "env": dict(spec.env)}


def _claude_text(entry: dict[str, Any]) -> str:
    lines = [
        f"{NAME}:",
        "  Scope: User config (available in all your projects)",
        "  Type: stdio",
        f"  Command: {entry['command']}",
        f"  Args: {' '.join(entry['args'])}",
        "  Environment:",
    ]
    lines += [f"    {key}={value}" for key, value in entry["env"].items()]
    return "\n".join(lines) + "\n\nTo remove this server, run: claude mcp remove\n"


@dataclass
class FakeClient:
    """A client with one entry. `get` exits 1 when the entry is absent, as the real
    CLIs do; `add` stores the spec it was given; `remove` deletes the entry."""

    client: str
    entry: dict[str, Any] | None
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: Any) -> Output:
        command = list(argv)
        self.calls.append(command)
        if command[1:3] == ["mcp", "get"]:
            if self.entry is None:
                return Output(1, "", "No MCP server found")
            if self.client == "claude":
                return Output(0, _claude_text(self.entry), "")
            payload = json.dumps({"transport": {"type": "stdio", **self.entry}})
            return Output(0, payload, "WARNING: a diagnostic on stderr")
        if command[1:3] == ["mcp", "add"]:
            self.entry = _entry_from(SPEC)
            return Output(0, "added", "")
        if command[1:3] == ["mcp", "remove"]:
            self.entry = None
            return Output(0, "removed", "")
        raise AssertionError(f"unexpected command {command}")


def _run(
    client: FakeClient,
    *,
    force: bool = False,
    yes: bool = False,
    ask: Any = None,
    present: bool = True,
) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = register(
        client.client,
        SPEC,
        run=client,
        which=lambda name: f"/bin/{name}" if present else None,
        out=lines.append,
        force=force,
        yes=yes,
        ask=ask,
    )
    return code, lines


def _mutations(client: FakeClient) -> list[list[str]]:
    return [c for c in client.calls if c[2:3] in (["add"], ["remove"])]


def test_the_claude_command_is_user_scoped_and_passes_the_env_and_the_server() -> None:
    assert add_command("claude", SPEC) == [
        "claude",
        "mcp",
        "add",
        "--scope",
        "user",
        NAME,
        "-e",
        "PYTHONPATH=/repo/src",
        "-e",
        "DOCKER_HOST=unix:///run/user/1000/podman/podman.sock",
        "--",
        "/repo/.venv/bin/python",
        "-m",
        "igs_iec104_mcp",
        "--project",
        "/proj",
        "--runtime-image",
        "sha256:aa",
        "--relay-image",
        "sha256:bb",
    ]
    assert remove_command("claude", NAME) == [
        "claude",
        "mcp",
        "remove",
        "--scope",
        "user",
        NAME,
    ]


def test_the_codex_command_uses_its_env_flag() -> None:
    command = add_command("codex", SPEC)
    assert command[:4] == ["codex", "mcp", "add", NAME]
    assert "--env" in command
    assert command[command.index("--") + 1] == "/repo/.venv/bin/python"
    assert remove_command("codex", NAME) == ["codex", "mcp", "remove", NAME]


def test_an_absent_entry_is_added_when_confirmed_with_yes() -> None:
    client = FakeClient("claude", entry=None)
    code, lines = _run(client, yes=True)
    assert code == 0
    assert client.entry == _entry_from(SPEC)
    assert _mutations(client) == [add_command("claude", SPEC)]
    assert any("is registered" in line for line in lines)


def test_an_absent_entry_is_added_when_the_user_confirms() -> None:
    client = FakeClient("codex", entry=None)
    code, _ = _run(client, ask=lambda prompt: "y")
    assert code == 0
    assert client.entry == _entry_from(SPEC)


def test_the_same_entry_changes_nothing() -> None:
    for name in ("claude", "codex"):
        client = FakeClient(name, entry=_entry_from(SPEC))
        code, lines = _run(client)
        assert code == 0, name
        assert _mutations(client) == [], name
        assert any("nothing changes" in line for line in lines)


def test_a_different_entry_is_kept_without_force() -> None:
    different = {**_entry_from(SPEC), "args": ["-m", "other"]}
    client = FakeClient("claude", entry=different)
    code, lines = _run(client, yes=True)
    assert code == 1
    assert client.entry == different
    assert _mutations(client) == []
    assert any("--force" in line and "arguments" in line for line in lines)


def test_a_different_entry_is_replaced_with_force_and_yes() -> None:
    client = FakeClient("codex", entry={**_entry_from(SPEC), "command": "/old/python"})
    code, _ = _run(client, force=True, yes=True)
    assert code == 0
    assert client.entry == _entry_from(SPEC)
    assert [c[2] for c in _mutations(client)] == ["remove", "add"]


def test_force_alone_does_not_skip_the_confirmation() -> None:
    client = FakeClient("claude", entry={**_entry_from(SPEC), "command": "/old"})
    code, lines = _run(client, force=True)
    assert code == 1
    assert _mutations(client) == []
    assert any("run again with --yes" in line for line in lines)


def test_a_declined_confirmation_changes_nothing() -> None:
    client = FakeClient("claude", entry=None)
    code, lines = _run(client, ask=lambda prompt: "n")
    assert code == 1
    assert _mutations(client) == []
    assert any("nothing changed" in line for line in lines)


def test_without_a_terminal_and_without_yes_nothing_runs() -> None:
    client = FakeClient("codex", entry=None)
    code, lines = _run(client, ask=None)
    assert code == 1
    assert _mutations(client) == []
    assert any("--yes" in line for line in lines)


def test_a_missing_client_cli_is_reported_and_nothing_runs() -> None:
    client = FakeClient("claude", entry=None)
    code, lines = _run(client, yes=True, present=False)
    assert code == 1
    assert client.calls == []
    assert any("not on PATH" in line for line in lines)


def test_environment_values_are_never_printed() -> None:
    secret = "unix:///SECRET-SOCKET-VALUE.sock"
    client = FakeClient(
        "claude",
        entry={**_entry_from(SPEC), "env": {**SPEC.env, "DOCKER_HOST": secret}},
    )
    code, lines = _run(client)
    assert code == 1
    assert "SECRET-SOCKET-VALUE" not in "\n".join(lines)
    assert any("environment" in line for line in lines)


def test_a_failed_add_is_reported_with_its_output() -> None:
    client = FakeClient("codex", entry=None)
    original = client.__call__

    def failing(argv: Any) -> Output:
        if list(argv)[1:3] == ["mcp", "add"]:
            client.calls.append(list(argv))
            return Output(2, "", "codex refused the server")
        return original(argv)

    lines: list[str] = []
    code = register(
        "codex",
        SPEC,
        run=failing,
        which=lambda name: f"/bin/{name}",
        out=lines.append,
        yes=True,
    )
    assert code == 1
    assert any("codex refused the server" in line for line in lines)


def test_the_generic_entry_has_the_command_the_arguments_and_the_env() -> None:
    assert generic_entry(SPEC) == {
        "command": "/repo/.venv/bin/python",
        "args": list(SPEC.args),
        "env": {
            "PYTHONPATH": "/repo/src",
            "DOCKER_HOST": "unix:///run/user/1000/podman/podman.sock",
        },
    }


def test_the_generic_text_carries_the_json_form() -> None:
    text = generic_text(SPEC)
    payload = json.loads(text[text.index("{") :])
    assert payload == {"mcpServers": {NAME: generic_entry(SPEC)}}
    assert "nothing was written" in text


def test_the_generic_mode_prints_and_never_runs_a_client(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def no_client(argv: Any) -> Output:
        raise AssertionError(f"the generic mode ran {argv}")

    code = main(
        [
            "--client",
            "generic",
            "--repository",
            "/repo",
            "--project-dir",
            "/proj",
            "--runtime-image",
            "sha256:aa",
            "--relay-image",
            "sha256:bb",
            "--docker-host",
            "unix:///run/user/1000/podman/podman.sock",
        ],
        run=no_client,
        which=lambda name: None,
    )
    assert code == 0
    assert '"mcpServers"' in capsys.readouterr().out
