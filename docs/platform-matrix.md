# Platform matrix and feature support

This page states what has been tested, on which platform, with which versions, and what the server does and does
not do. It is a statement of the server's own tests and of observed behaviour. It is not a certification of any
station or client, and a passing test with two endpoints built on the same library shows that the test bench works,
not that the protocol behaves as the standard requires.

## Tested platforms

| Platform | Engine | What ran | Result |
| --- | --- | --- | --- |
| Linux, GitHub-hosted runner (`ubuntu-latest`) | Docker Engine | the Python gate, the Rust gate, the runtime image, and the integration tests with both images built in CI | passing, run 38230065120 (commit `ed12501`) |
| Linux on WSL2 (Ubuntu 26.04, kernel 6.18) | Podman 5.7.0, rootless, Docker-compatible socket | the full integration suite with the release runtime binary | 35 passed, none skipped (V2c, 2026-10-12) |
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
| MCP SDK | `mcp` 2.3.0 (`uv.lock`) |

## Feature support

| Feature | Status | Notes |
| --- | --- | --- |
| Client: STARTDT, STOPDT, the transfer state | supported and tested | `start_transfer`, `wait_transfer_started` |
| Client: general interrogation | supported and tested | `general_interrogation`; the termination ends the step |
| Client: reads, counter interrogation, clock sync, test command | supported by the runtime; not in scenario actions | the scenario layer does not drive them yet |
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
| Switchover on Docker | supported and tested | `switchover`, and a stop of a group's station |
| Manual switchover through the control API | not exposed | the library provides it; the control API does not |
| Group transfer start (`POST /v1/transfer/start`) | accepted, no effect | the group starts the transfer of the station it starts |
| Event-log overflow | supported | a drop is a gap in the run and its report |
| Published station (external client reaches a managed station) | supported on Linux | Docker Desktop check open |
| External target policy (validation, operations, opt-in) | supported and tested | `target_validate`; the probe sends no IEC frame |
| Confirmation of state-changing operations | supported | MCP elicitation; a reviewed opt-in when the client cannot be asked |
| Run records, reports, retention | supported and tested | JSON, JUnit XML, Markdown |
| Recovery after a server crash | supported | the run is `interrupted`; its bench is listed and removed by its run |
| Streamable HTTP transport | **not implemented** | stdio only; the HTTP transport waits for a maintainer's review (M4) |
| Controlled faults (interruption, latency) | **not implemented** | waits for a maintainer's approval of the component (V3) |
| Interoperability with an independent implementation | **not done** | waits for a maintainer's decision (X3) |

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
2. The remaining tasks of the plan: M4 (Streamable HTTP), X3 (interoperability), and V3 (controlled faults). Each
   needs a maintainer's decision first.
3. A review of this matrix and of the limitations by a maintainer, and of the licences (`scripts/check_licences.py`
   and `cargo deny`).

Until then, this matrix describes a development release. It is not a stable release.
