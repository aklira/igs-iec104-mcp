# Platform matrix and feature support

This page states what has been tested, on which platform, with which versions, and what the server does and does
not do. It is a statement of the server's own tests and of observed behaviour. It is not a certification of any
station or client, and a passing test with two endpoints built on the same library shows that the test bench works,
not that the protocol behaves as the standard requires.

## Tested platforms

| Platform | Engine | What ran | Result |
| --- | --- | --- | --- |
| Linux, GitHub-hosted runner (`ubuntu-latest`) | Docker Engine | the Python gate, the Rust gate, the runtime image, and the integration tests with both images built in CI | passing, run 38230065120 (commit `ed12501`) |
| Linux, GitHub-hosted runner (`ubuntu-latest`), fresh checkout | Docker Engine | the job `install`: the installer twice, the state's identities, the server's handshake, the example bench with its cleanup checked, and the registration tests | passing, pull request #2 (commit `c32d2ae`) |
| Linux on WSL2 (Ubuntu 26.04, kernel 6.18) | Podman 5.7.0, rootless, Docker-compatible socket | the full integration suite with the release runtime binary | 35 passed, none skipped (V2c, 2026-10-12) |
| Linux on WSL2 (Ubuntu 26.04, kernel 6.18) | Podman 5.7.0, rootless | the installation of [install-agent](install-agent.md) on a clone of the commit: the installer, the approval stop, the level 1 and level 2 checks, and the registration with the real Claude Code CLI | passed (I8, 2026-10-14) |
| Windows with Docker Desktop (WSL2 backend) | Docker Desktop | not run | **open**: needed before a release |
| macOS with Docker Desktop | Docker Desktop | not run | not tested |
| Windows without WSL, or native Windows | any | not run | not supported |

The Docker Desktop checks are the open item of the platform matrix. They must run on a machine that has Docker
Desktop, with the same test suite, before a release.

## Versions

| Component | Version |
| --- | --- |
| Python | 3.12 (`.python-version`) |
| Rust | 1.99.0 (`runtime/rust-toolchain.toml`) |
| Upstream library `igs-iec104` | revision `e91ecc824fcbc750262a5c8505c8812457d200c1` (`runtime/Cargo.toml`) |
| Runtime image base | `rust:1.99.0-trixie` to build, `debian:trixie-slim` to run |
| Relay image base | a Python image pinned by its digest |
| Podman (tested) | 5.7.0 |
| Claude Code CLI (tested) | 2.1.232 (registration and connection checked) |
| Codex CLI (tested) | 0.147.0 (registration and a tool call checked) |
| uv (installer and tests) | 0.13.1 |
| MCP SDK | `mcp` 2.3.0 (`uv.lock`) |

## Feature support

| Feature | Status | Notes |
| --- | --- | --- |
| Client: STARTDT, STOPDT, the transfer state | supported and tested | `start_transfer`, `wait_transfer_started` |
| Client: general interrogation | supported and tested | `general_interrogation`; the termination ends the step |
| Client: counter interrogation | supported by the runtime (`POST /v1/interrogation/counter`); not a scenario action | the scenario layer does not drive it yet |
| Client: reads, clock sync, test command | not exposed by the runtime's control API | no route in the runtime's control API |
| Client: point image and spontaneous updates | supported and tested | `assert_point`, `expect_point`, cause checks |
| Station: points and their kinds | supported and tested | the kinds of the upstream `PointValue` |
| Station: simulation (`toggle`, `ramp`, `walk`) | supported | a seeded change at each period |
| Station: command acceptance and refusal | supported by the runtime | the policy is set per point in the definition; a command is not sent by a scenario yet |
| Commands to a managed instance | **not supported** | `send_command` to a managed instance is unsupported |
| Commands to an external target | **not supported** | confirmed by a person first, then unsupported: external connections are not made yet |
| Reconnect after a station restart | supported on a local bench | the restart test runs on the local bench; a managed station is not restarted |
| Stop of a managed station | supported, for good | `stop_instance`; its logs are kept; a restart is refused |
| TLS on both roles | supported and tested | upstream settings; acceptance and rejection scenarios; a refused client records a security alarm |
| Redundancy group of a client (2 to 8 stations) | supported and tested | the group starts one station and switches to another |
| Several clients on one station | one client's transfer at a time: the last one started (the upstream redundancy rule); the others are closed | reproduced on the clean install, 2026-10-14 (issue #4); the scenario check refuses an order that cannot pass ([docker](docker.md#one-station-several-clients)) |
| Connection through a fault proxy | supported on Docker and tested | `fault_proxy: true`; `inject_fault` (interrupt, latency) and `clear_fault`, with a reconnection after the clear |
| Switchover on Docker | supported and tested | `switchover`, and a stop of a group's station |
| Manual switchover through the control API | not exposed | the library provides it; the control API does not |
| Group transfer start (`POST /v1/transfer/start`) | accepted, no effect | the group starts the transfer of the station it starts |
| Event-log overflow | supported | a drop is a gap in the run and its report |
| Published station (external client reaches a managed station) | supported on Linux; the host port is chosen by the engine (D-054) | the endpoint is in the run's `published` event; Docker Desktop check open |
| External target policy (validation, operations, opt-in) | supported and tested | `target_validate`; the probe sends no IEC frame |
| Confirmation of state-changing operations | supported | MCP elicitation; a reviewed opt-in when the client cannot be asked |
| Run records, reports, retention | supported and tested | JSON, JUnit XML, Markdown |
| Recovery after a server crash | supported | the run is `interrupted`; its bench is listed and removed by its run |
| Streamable HTTP transport | supported on the loopback, with a token | `--transport http`; no remote listener and no TLS yet (see [setup](setup.md#http-transport)) |
| Controlled faults (interruption, latency) | supported on Docker and tested | the interrupt and latency scenarios of V3c (D-051); a stop of the proxy's own container is not tested |
| Interoperability with an independent implementation (OpenMUC j60870 1.8.0, black box) | supported in both directions on the bench network, tested | `tests/integration/test_interop_j60870.py` (our client, their station) and `test_interop_j60870_client.py` (their client, our station); a client outside Docker is not yet tested against the peer (D-055) |

| Installation with `scripts/install.sh` | supported on Linux and WSL2 | the steps, the state, and the approvals of [installing with a coding agent](install-agent.md); the registration is never run in CI, which has no client CLI |
| Registration with Claude Code and Codex | supported and tested with the real CLIs | `--client claude` (user scope) and `--client codex` (global); `--client generic` prints the entry and writes nothing, checked by a handshake of the printed command |
| Preflight and images check (`doctor`) | supported | read-only, with a temporary project for the handshake; a missing installation image is a warning; `--run-bench` starts containers |
| A real Codex session calling the server | tested with Codex CLI 0.147.0 (2026-10-14) | Codex asks before each tool call of an MCP server; a session without a person must configure that approval itself, which the installer does not do |

### What the runtime does not handle

These limits come from the upstream library's support matrix at the pinned revision (`docs/capabilities.md`). A
station refuses these requests with a negative confirmation; a client can send them only through the library's
generic path, which the scenarios do not use.

- Bitstring command, reset process, parameters, and file transfer are not handled by the station.
- Counter freeze and counter reset are refused.
- An interrogation with a qualifier the station does not serve is refused with a negative confirmation.
- A deactivation request is refused.
- Packed single points with status-change detection, and normalized values without quality, are not published.

## Limitations observed

- A group station must be connected before a scenario stops it. The step waits for that station's loss; it does not
  read the group's history, so a stop of a station that the group never connected to times out.
- Two clients on one station: the one that started its transfer last receives the data, and the other is closed. A
  step that needs the closed client's data times out. The scenario check refuses the order when the steps make it
  certain; a `parallel` step or a redundancy group is not judged.
- A client's TLS security events are discarded by the runtime. A station's security alarms are recorded.
- A bench that connects a client to an external target is refused by the planner. Its message names a task that is
  done; the connection is not supported yet.
- A managed station is not restarted. The restart needs a fixed station port, which the plan does not impose.
- Only the default cleanup policy is run (`remove: success_and_failure`, `keep_evidence: true`).
- A bench cannot be updated in place: a changed bench is stored under a new name. A scenario can be updated.
- The package has no console script. Start the server with `python -m igs_iec104_mcp` from the source tree (see
  [setup](setup.md#start-the-server)).

## Before a release

The release is gated on these items, in this order:

1. The Docker Desktop checks above, on a machine with Docker Desktop.
2. The remaining tasks of the plan: V5, the release decision of a maintainer. X3 (interoperability, D-055), V3
   (controlled faults, D-051), and Phase I (the agent-friendly installation, issue #1) are done. Phase I is confirmed on
   `main` once its pull request is merged.
3. A review of this matrix and of the limitations by a maintainer, and of the licences (`scripts/check_licences.py`
   and `cargo deny`).

Until then, this matrix describes a development release. It is not a stable release.
