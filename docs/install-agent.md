# Installing the server with a coding agent

This page is the entry point for a coding agent (Claude Code, Codex, or another LLM with terminal access) that
is asked to install `igs-iec104-mcp` from this repository. It gives the order of the steps, the output to expect
at each one, and the points where the agent must stop and ask a person. The manual reference is
[setup](setup.md); the problems you may meet are in [troubleshooting](troubleshooting.md).

The installer does not do privileged work. It never runs `sudo`, installs a system package, or changes a group.
When a prerequisite is missing, the preflight names the command that fixes it, and a person runs that command.

## What "installed" means

There are two levels. Reach the first, then the second.

| Level | Meaning | How to check it |
| --- | --- | --- |
| 1. Connected | The server starts, and its tools can be discovered and used. No container is started. | `scripts/install.sh` ends with `installed:`, and the server checks of the doctor pass (step 5). |
| 2. Operational | The example bench runs on Podman or Docker: a scenario passes, and its bench is removed afterwards. | `doctor --run-bench` reports `bench-run ok` and `bench-cleanup ok` (step 6). |

Level 1 needs no container. Level 2 starts containers, so it needs the person's approval.

## Supported platforms

| Platform | Status |
| --- | --- |
| Linux, with Podman 5 (rootless) | Tested on WSL2 (Ubuntu, kernel 6.18), with Podman 5.7.0. This is the first engine. |
| Linux, with Docker Engine | Tested in CI on GitHub-hosted Linux runners. It is the second engine. |
| Windows, through WSL2 | Tested as Linux inside WSL2. Run every command inside the WSL2 distribution. |
| Docker Desktop (WSL2 or macOS) | Not verified yet. The platform matrix lists it as open. |
| Native Windows, macOS | Not supported. The installer stops on a host other than Linux. |

Use [the platform matrix](platform-matrix.md) for the versions behind each row.

The installer registers the server with Claude Code and Codex (`--client claude` or `--client codex`). For any other
client, `--client generic` prints the server's entry in the common `mcpServers` form, and writes nothing: add it to the
client's configuration yourself, as [setup](setup.md#other-clients) describes. Some clients name the keys differently,
so adjust them to the client's format.

## Prerequisites

The preflight checks each prerequisite. Run step 1 first, and fix what it reports.

| Prerequisite | Why | Who installs it |
| --- | --- | --- |
| `uv` | the locked Python environment | the person, from <https://docs.astral.sh/uv/> |
| Python 3.12 | the server and the tests (`.python-version`) | `uv` can download it when it is missing |
| Rust 1.99.0 | builds the runtime (the version is pinned in `runtime/rust-toolchain.toml`) | the person, with rustup |
| `pkg-config` and the OpenSSL development files | the runtime's TLS backend | the person: on Debian or Ubuntu, `sudo apt install pkg-config libssl-dev` |
| Podman 5 with its socket running, or Docker Engine | the benches | the person; see [Podman](setup.md#podman) |
| `claude` or `codex` | registers the server with the client (step 3) | the person, from the client's own installation |

The agent does not run `sudo`. It shows the command from the preflight, and waits for the person to run it.

## Steps

Run every command from the root of this repository. The examples use `~/igs-project` as the project directory.

### 1. Check the plan, and change nothing

```bash
scripts/install.sh --dry-run --project-dir ~/igs-project --client claude
```

Expected output starts with `dry run: nothing is changed.` and lists eight steps. Replace `claude` with `codex`
for Codex. If the output is not this, stop and read the error.

### 2. Start the Podman service

Skip this step if `podman info` already works for the current user. Otherwise start the service, as
[setup](setup.md#podman) describes:

```bash
mkdir -p /run/user/$(id -u)/podman
podman system service --time=0 unix:///run/user/$(id -u)/podman/podman.sock &
```

The service must keep running while the benches run. Leave it running in its own terminal or process.

### 3. Install

```bash
scripts/install.sh --project-dir ~/igs-project --client claude
```

The steps appear in this order, and each one should succeed:

```text
==> environment
==> preflight
igs-iec104-mcp preflight
  ok    host           ...
  ...
  ok    engine         podman 5.7.0 at unix:///run/user/1000/podman/podman.sock
  ...
  ok    bench-validate the example bench validates
All required checks passed.
==> images, built with podman
==> project directory
==> state
state written: ...
==> registration with claude
This will run:
  claude mcp add --scope user igs-iec104 -e PYTHONPATH=... -e DOCKER_HOST=... -- ...
```

At the registration step, the installer prints the exact command and waits for an answer. An agent has no
terminal, so the installer stops with `not a terminal: run again with --yes to confirm`. **Stop here and show the
printed command to the person.** Continue only when the person approves it. Then run the same command with
`--yes` added:

```bash
scripts/install.sh --project-dir ~/igs-project --client claude --yes
```

The last lines are:

```text
claude: igs-iec104 is registered
installed: runtime sha256:...
           relay   sha256:...
           project /home/.../igs-project
next: start a new claude session, and check the server (docs/setup.md#connect-an-mcp-client)
```

A second run prints `state unchanged` and `registered as specified; nothing changes`, and changes nothing.

### 4. What the registration changes

The installer registers the server in the user scope of Claude Code, or in the global configuration of Codex. It
never edits a configuration file itself, and it never writes into a repository. If an entry named `igs-iec104`
already exists and differs, the installer prints the names of the fields that differ and changes nothing. **Stop
and ask the person** before a run with `--force`, which replaces that entry. The environment values of an existing entry are never
printed: the clients print them in clear, and the installer does not repeat them.

### 5. Check level 1: the server connects

```bash
PYTHONPATH=src uv run --locked python -m igs_iec104_mcp.doctor --server-only
```

Expected output: `server ok`, `tools ok` (19 tools), `bench-validate ok`, and `All required checks passed.`
with exit code 0. The preflight also reports `images`: `ok` once the installer has built the images, or `warn` with the
command to run when they are missing. This check starts the server, lists its tools, and validates the example bench. It starts no
container and stores nothing.

Then check the registration with the client:

```bash
claude mcp get igs-iec104     # or: codex mcp list
```

### 6. Check level 2: the bench runs (needs approval)

This step starts containers on the engine, and removes them when it ends. **Ask the person first.**

```bash
PYTHONPATH=src uv run --locked python -m igs_iec104_mcp.doctor --run-bench
```

Expected output, with exit code 0:

```text
  ok    bench-run      run <id> passed: 6 of 6 verdicts
  ok    bench-cleanup  no resource of run <id> is left on the engine
```

If `bench-run` fails, the temporary project of the run is kept, and its path is in the output. The
`bench-cleanup` check still says whether the bench was removed.

### 7. Use the server from the client

Start a new session of the client, and ask it for the tools of `igs-iec104`. In Claude Code, `/mcp` shows the
state of the server. Codex: `codex mcp list` lists it.

Codex asks for approval before each tool call of an MCP server. A session that runs without a person cannot
give that approval, so it must be configured by a person, for example with `default_tools_approval_mode` for this server.
The installer does not set it.

## Stop and ask the person

Stop, and ask before you do any of these:

- run a command with `sudo`, install a system package, or change a group. The preflight names the command; the
  person runs it.
- run the installer with `--yes` for a registration, before the person approved the printed command;
- run it with `--force`, which replaces an entry that differs;
- run `doctor --run-bench`, which starts containers;
- remove resources with `bench_recovered_remove`, or delete a project directory.

The installation contacts no external IEC 104 target. Nothing in it commits or pushes to a repository.

## Exit codes

| Command | Code | Meaning |
| --- | --- | --- |
| `scripts/install.sh` | 0 | the steps completed, and the registration (if any) is as specified |
| `scripts/install.sh` | 1 | a check failed, a build failed, or the registration was refused |
| `scripts/install.sh` | 2 | a usage error, or `uv` is not on the path |
| `doctor` | 0 | no required check failed (warnings are reported) |
| `doctor` | 1 | a required check failed; its action is printed under it |

## When something goes wrong

| What you see | What to do |
| --- | --- |
| `no container engine answers` | Start the Podman service (step 2), or install Docker Engine. Set `DOCKER_HOST` if the socket is elsewhere. |
| `may not use the socket` | Ask an administrator for access. Do not change groups yourself. |
| `uv not found` | The person installs `uv`. |
| `rustc does not run` | Check the Rust installation with `rustup show`, then run the doctor again. |
| `OpenSSL development files not found` | The person runs `sudo apt install pkg-config libssl-dev` (Debian or Ubuntu). |
| `the runtime pins 1.99.0` (a warning) | `rustup toolchain install 1.99.0`. |
| `no images are known` | Run the installer (step 3) first. |
| `no image 'sha256:...' on the engine` | The images were built under another user, or another `HOME`: Podman keeps its images per user. Run the installer, the doctor, and the engine service as the same user with the same `HOME`. |
| `the installed images are for ...` | Run the installer again with the engine the message names. |
| `not a terminal: run again with --yes` | The registration is waiting for approval (step 3). |
| `is registered differently` | Show the person the differing fields. Replace the entry only with approval (step 4). |
| `the server did not complete the handshake` | Run the server by hand, as the output says, and read its error. |
| `bench-cleanup` fails | List what is left with `bench_recovered`, then remove it with `bench_recovered_remove`, after approval. |
| `--client must be claude, codex, or generic` | Use one of the three names. |

For the server itself, see [troubleshooting](troubleshooting.md).
