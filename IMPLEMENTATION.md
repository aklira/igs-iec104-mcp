# Implementation Plan

## 1. Purpose and agreed scope

Build a Python MCP server that orchestrates IEC 60870-5-104 simulation, integration, and validation test benches using the Rust library `aklira/igs-iec104`.

The first complete release must support:

- Deployment of IEC 104 clients and servers as Docker containers.
- Explicitly configured external IEC 104 clients and servers.
- Version-controlled YAML test bench definitions, instance configurations, and test scenarios.
- MCP tools to create, inspect, update, validate, and execute those definitions.
- Scenario execution with protocol assertions, controlled failures, cancellation, and reports.
- Reproducible runs with preserved configurations, dependency versions, and evidence.

There are no specified capacity, host-platform, or MCP-client constraints. Establish and document a tested platform matrix and measured capacity rather than promising unlimited scale. Start with a single Docker Engine and Linux containers; validate Linux and Docker Desktop/WSL networking during integration.

## 2. Working rules

These rules apply to every task of §14. Read this file before any task; read only the inputs the task lists.

### Process

- One task at a time, from §14, in dependency order. Keep a change under about 400 lines; split a larger task.
- Commit only when the maintainer asks for it; push only when the maintainer asks for it. Commit message: `<area>: <what>`, a blank line, then `Task: <ID>`. Tick the task's box in §16 in the commit where the task is done, and only then.
- Tasks marked ⛔ are human gates: stop and ask a maintainer.
- Record each task's prompt and summary in `docs/ai-log/<task-id>.md`. Record open questions in `docs/ai-log/QUESTIONS.md` and resolved decisions in `DECISIONS.md` (`D-001`, ...) with a permanent test where a test can show the decision.
- Everything committed is in English.

### Legal and clean room

- Never copy IEC standard text into code, comments, docs, tests, YAML examples, or commit messages. Cite clauses (`IEC 60870-5-104 §7.5`) and paraphrase. Type identifications, causes, field sizes, and bit positions are data and may be used.
- Never open, quote, reconstruct, or describe the source of other IEC 104 implementations (lib60870 in C or .NET, the crates `iec60870-5` and `lib60870`, or any other). Independent implementations are used as black boxes only: a separate process or container, driven over the network.
- Dependencies must be MIT, Apache-2.0, BSD, or ISC (the Python and Rust checks of §11 enforce it). Decision D-007 also admits MPL-2.0, PSF-2.0, and MIT-0, but only for transitive and development dependencies used unmodified; a direct dependency under those licences needs approval. Ask before adding a dependency that §11 does not list.
- Secrets (control credentials, private keys, passwords) are never committed, logged, or written into reports.

### Standard gate

A task is done when these pass, once the tooling exists (tasks B1 and B2):

```bash
uv run ruff format --check
uv run ruff check
uv run mypy
uv run pytest tests/unit
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cargo deny check
```

Docker integration tests (`tests/integration`) and end-to-end tests (`tests/e2e`) run in CI on Linux, and locally when the task touches Docker or the MCP surface.

## 3. Architecture

Separate orchestration from protocol execution:

```text
MCP host
    |
    v
Python MCP server
    +-- Definition validation and storage
    +-- Docker test bench manager
    +-- Asynchronous scenario engine
    +-- Runtime API client and event collection
    +-- Run state, artifacts, and reports
            |
            v
    Rust IEC 104 runtimes in Docker
            |
            +-- Managed clients and servers
            +-- Explicit external IEC 104 targets
```

### Python control plane

Use the official MCP Python SDK, asyncio, typed validation models, a Docker Engine API adapter, and an asynchronous HTTP client. Pin dependency versions and commit a lockfile.

Keep MCP handlers thin: business logic must also be callable by integration tests and a command-line scenario runner. Isolate blocking Docker calls from the asyncio event loop.

Support stdio initially, keeping stdout exclusively for MCP messages. Add Streamable HTTP for shared deployments, with authentication and access control before exposing a remote control plane.

### Rust protocol runtime

Create one binary, `igs-iec104-runtime`, configurable as client or server. Integrate `igs-iec104` directly rather than parsing the existing CLI output or introducing Python bindings in the first release.

Expose a versioned internal HTTP API and a replayable event stream. The API supports readiness, state inspection, protocol actions, point updates, event retrieval, and shutdown. Keep it separate from IEC 104 traffic and accessible only to the controller. Use a control credential for each test bench and exclude it from logs and reports.

The Python controller must have a documented route to runtime control endpoints. For a host-based controller, publish control ports on loopback; for a containerized controller, attach it to a dedicated control network. Do not assume Docker container DNS names resolve on the host.

The runtime API contract is written before the code that serves it (task C1): an OpenAPI document for the HTTP API and a JSON Schema for events, both versioned in `contracts/`. The Python client and the Rust server are tested against it.

## 4. Upstream integration

### Revision and build

The upstream revision to pin is `e91ecc8` or later: it is the first revision with all the upstream prerequisites (U1 to U4). Confirm the revision at bootstrap (task B2), pin its full hash, and record it in `DECISIONS.md`.

- Depend on `igs-iec104` with a Git dependency pinned by `rev`. The crates `igs-iec104-codec` and `igs-iec104-link` come from the same workspace and must resolve to the same revision; do not mix a Git and a crates.io source.
- The upstream minimum supported Rust version is 1.85. Pin the toolchain in `rust-toolchain.toml` at that version or later.
- The upstream default TLS backend is OpenSSL (feature `openssl`), which needs the OpenSSL development files at build time and the OpenSSL runtime library in the image. The `rustls` feature lacks mandatory items of the IEC 62351-3 profile (see the upstream `CHANGELOG.md`); use it only if a decision records why.
- Record the upstream revision and the image digest in every run manifest.

### What the library provides, and what it does not

Facts checked against the pinned revision:

- The demo server `igs104-server` does not provide TLS or live point-file reload. The library provides TLS configuration (`TlsSettings`) and point updates (`ServerHandle::add_point`, `set_group`, `update`, `value`). The runtime uses the library, not the demo server.
- Client request methods accept work into a bounded command channel. `Ok(())` means queued: not sent, not confirmed.
- By default, a request made while the connection is down waits and is sent on the next connection. With `ClientConfig::with_offline_requests(OfflineRequests::Discard)`, the client drops the requests still queued when a connection ends and those made until the next connection opens, and reports each one as `Event::Discarded`. The runtime uses `Discard`.
- A session of the controlling role reports `Delivery::Transmitted(asdu)` once the I frame of a request is written, before any answer to it. A request made while the next connection opens may still be sent on it; `Transmitted` tells.
- Every delivery of a connection comes before its `Disconnected` event. So a request left on the connection of the last `Connected` event before its `Transmitted`, and an ASDU belongs to the connection of the last `Connected` before it.
- `Client::next_event` takes `&mut self`: one owner task per client reads the events. The upstream `Client` documentation shows the owner pattern.
- `igs_iec104::answer::judge` matches an ASDU to a request by type identification, common address, information object address, and expected cause, and reports confirmation, negative confirmation, or a refusal cause of 44 to 47. It does not know the connection generation; the runtime adds it.
- The client keeps no values. The runtime builds its own client-side point image from the received ASDUs (§7).
- The upstream server does not re-send the answers of a closed connection on the next one (upstream decision D-021); it does re-send unacknowledged events. Independent implementations may behave differently, so the runtime must not rely on D-021 for correlation.
- `RedundantClient` hands the unacknowledged ASDUs of a lost connection to the application and does not re-send them itself.
- `Server::run_until(stop, grace)` stops a station in order: no new connection, each connection sends what it has taken and closes, and those still open after `grace` are closed by force. It returns a `ShutdownReport` (connections open at the stop, closed by force, events still held). Dropping the `run` future closes the connections at once.
- With `ServerConfig::with_events(capacity)`, `Server::events()` gives a bounded stream of `ServerEvent`: `Connected { id, peer }`, `Transfer { id, state }`, `Superseded { id, by }`, and `Closed { id, end }` with a `ConnectionEnd` (station, transport error, or forced). A full stream drops the event and counts it (`ServerEvents::dropped`); the station never waits. The runtime reports a nonzero drop count as an evidence gap.
- The answers the station sends to commands are not in that stream. Commands are visible through the `Handler`, whose methods are synchronous and must not block; a handler forwards them with a non-blocking `try_send`.
- `docs/capabilities.md` upstream lists the procedures and type identifications that the client and the station support, with the test that covers each, and what is not supported (for example: commands need select-before-operate, counter freeze and reset are refused, the bitstring command, reset process, parameters and file transfer are not handled by the station). The runtime's action matrix follows it.

### Upstream prerequisites

These extensions were added to `igs-iec104` (its phase 6) for this project. They are all merged at `e91ecc8`.

| ID | Extension | Upstream commits | Used by |
| --- | --- | --- | --- |
| U1 | Graceful server shutdown: `Server::run_until`, `ShutdownReport` | `9b8bbef` | C3, D2 |
| U2 | Server connection events: `ServerConfig::with_events`, `ServerEvent`, `ConnectionEnd` | `e91ecc8` | C3, E5, V2 |
| U3 | Ordered client events; `OfflineRequests::Discard` with `Event::Discarded`; `Delivery::Transmitted` | `d09ef39`, `6fc8ba0`, `abb4fd8` | C2 |
| U4 | Supported action matrix: `docs/capabilities.md` | `bc82f54` | C2, C3 |

A later upstream change to one of these APIs is a change of the pinned revision, recorded in `DECISIONS.md`.

## 5. Domain models and YAML contracts

Define versioned schemas with strict validation and actionable errors:

| Model | Responsibility |
| --- | --- |
| Test bench | Instances, networks, connections, external targets, and cleanup policy |
| Instance | Client/server role, IEC 104 settings, points, simulation, and resource limits |
| External target | Role, endpoint, common address, TLS references, and allowed operations |
| Scenario | Preconditions, ordered/parallel steps, assertions, deadlines, and cleanup |
| Run | Immutable input snapshot, status, progress, observations, verdicts, and artifacts |

Include `schema_version` in every definition. Reject unknown fields, invalid address ranges, duplicate points, incompatible point types and values, invalid timing/window parameters, unresolved references, and unsupported actions.

Take the valid ranges from the upstream library rather than restating them from memory: common address 1 to 65534 (65535 is the global address), information object address within three octets, and the link parameters (k, w, t0 to t3) validated by the upstream `Parameters` rules. Point types are the monitoring types of the upstream `PointValue`.

Model container state, runtime readiness, TCP connection state, and IEC 104 transfer state separately. Include connection generations so events from an earlier connection cannot complete a later request.

Use safe YAML loading without executable tags. Store definitions within an explicit project directory, prevent path traversal, use atomic writes, and check expected revisions when updating definitions. Snapshot all inputs at run start so editing a scenario does not change a running test.

Keep certificate keys and credentials as secret references rather than inline YAML or report content. Version scenario files in Git through the normal development workflow; MCP updates write reviewable files and do not automatically commit or push them.

## 6. Docker test bench management

Implement the following lifecycle:

1. Validate definitions and check Docker availability, images, mounts, and endpoint conflicts.
2. Create labeled networks and per-run configuration/artifact directories.
3. Start server runtimes, wait for application readiness, then start clients.
4. Resolve Docker peer names inside runtimes and establish configured connections.
5. Track runtime state, Docker events, logs, and resource usage.
6. Stop gracefully with a deadline and force termination if necessary.
7. Preserve evidence, then clean up resources owned by the test bench.

Assign project, test bench, run, and instance labels to every managed resource. Operations must be idempotent, roll back partial creation, and never remove unrelated Docker resources.

Use internal data networks for self-contained test benches. Enable external connectivity only for test benches declaring external targets. Publish a simulated server's IEC 104 port when an external client must connect; provide the reachable advertised endpoint explicitly.

Let the runtime bind its listening ports itself (port 0 where the port is not imposed) and report the bound address through its readiness endpoint. Do not pick a free port on the host and release it before the runtime binds it: two runtimes can then receive the same port (the upstream test suite had this race).

External targets are unmanaged: the controller may perform declared protocol operations but must not assume it can restart, reconfigure, or destroy them. Enforce target and operation policies in the control plane; apply network egress restrictions when actual isolation is required.

Run protocol containers without the Docker socket or privileged mode. Apply configurable CPU, memory, process, event-buffer, and execution limits. Build runtime images ahead of execution and record their immutable digests.

## 7. Runtime actions and events

Provide client actions for STARTDT/STOPDT, general/group interrogation, reads, counter interrogation, supported commands/setpoints, clock synchronization, and test commands, as the upstream matrix (`docs/capabilities.md`, U4) lists them at the pinned revision.

Provide server actions for inspecting and updating points, simulation control, and configured command acceptance/refusal behavior. The configured behavior is a policy known in advance (accept, refuse, refuse as an unknown address, per point or per command type), because the upstream `Handler` decides synchronously.

Use one owner task for each client event stream. Route requests through bounded channels and correlate responses using connection generation, ASDU type, common address, IOA, and expected confirmation stage (`answer::judge` plus the generation). Serialize operations whose responses cannot be distinguished reliably.

Connection generations and commands:

- Each client runs with `OfflineRequests::Discard`. The generation is a counter of the `Connected` events of the client, kept by its owner task; the upstream events are ordered, so every delivery belongs to the generation of the last `Connected` before it.
- A request is tied to a generation by its `Delivery::Transmitted` event, not by the moment it was queued. Before its `Transmitted`, a request is `pending`; a request reported as `Event::Discarded` is `not sent`, which is not a protocol verdict.
- Answers are judged with `answer::judge` only within the generation of the request's transmission. A command whose generation ended before its answer arrived is `indeterminate`, and is never retried.
- The runtime still accepts a command action only while the transfer is `Started`, so that a scenario does not depend on a request discarded at once.

Maintain a client-side point image in the runtime: the last value, quality, time tag, cause, and observation time of each point received, with the generation it arrived in. An interrogation is complete when its activation termination (cause 10) is judged; a station that never terminates it times out. Assertions on point values read this image only for observations after the step's cursor (§8).

Return separate action acceptance and eventual results. Verdicts must distinguish confirmed, refused, timed out, cancelled, and indeterminate. Never automatically replay an uncertain command after reconnecting.

Events include sequence number, instance ID, connection generation, UTC observation time, event kind, and relevant decoded IEC 104 fields. Use monotonic time for deadlines. Detect sequence gaps and buffer overflow explicitly; do not silently discard evidence used by assertions.

## 8. Scenario engine

Use declarative YAML actions; do not allow arbitrary shell commands or executable Python in scenarios.

Initial primitives:

- Wait for readiness, connection, or active data transfer.
- Perform interrogation/read procedures and wait for completion.
- Update simulated points and observe spontaneous delivery.
- Send commands, including select-before-operate, and assert confirmation/refusal.
- Assert value, quality, type, common address, IOA, cause, and timestamp conditions.
- Stop/restart managed instances and verify reconnect behavior.
- Run branches in parallel with explicit barriers.
- Execute cleanup steps after success, failure, or cancellation.

Capture an event cursor before triggering an action. Assertions consume observations after that cursor, avoiding races and stale-value matches. Support tolerances for floating-point values and timestamps, per-step deadlines, a global deadline, and configurable fail-fast behavior.

Make simulations reproducible using explicit initial values, seeds, update sequences, and periods. Real network scheduling is not deterministic; use bounded timing assertions. Time acceleration is outside the initial scope.

Add controlled network interruption, latency, and redundancy scenarios after core execution is reliable. Choose a dedicated proxy or narrowly scoped fault-injection component; do not grant broad privileges to all runtimes.

Example proposed syntax:

```yaml
schema_version: 1
name: interrogation-and-spontaneous-update
bench: basic-pair
steps:
  - action: wait_transfer_started
    client: client-1
    timeout: 10s
  - action: general_interrogation
    client: client-1
    timeout: 5s
  - action: assert_point
    client: client-1
    ioa: 100
    type: float
    value: 12.5
    tolerance: 0.001
  - action: update_point
    server: server-1
    ioa: 100
    value: 18.0
  - action: expect_point
    client: client-1
    ioa: 100
    type: float
    value: 18.0
    tolerance: 0.001
    cause: spontaneous
    timeout: 2s
```

`assert_point` reads the client-side point image (§7) as it stands after the interrogation completed; `expect_point` waits for a new observation after the step's cursor. Ensure the fixture `basic-pair` explicitly defines IOA 100 as a short floating point measured value with its initial value. Finalize this syntax with the schema implementation.

## 9. MCP surface

| Proposed tools | Responsibility |
| --- | --- |
| `bench_validate`, `bench_create`, `bench_get`, `bench_list` | Validate and register test bench definitions |
| `bench_start`, `bench_stop`, `bench_destroy` | Manage test bench resources |
| `instance_get`, `instance_logs` | Inspect instance state and bounded logs |
| `target_validate` | Validate configuration; optional explicit connectivity probe |
| `scenario_create`, `scenario_update`, `scenario_get`, `scenario_list`, `scenario_validate` | Manage YAML scenarios |
| `scenario_run` | Launch a validated scenario and return a run ID |
| `run_get`, `run_events`, `run_cancel`, `run_report` | Track progress, inspect evidence, cancel, and obtain reports |

Return typed structured results, stable error codes, identifiers, pagination cursors, and artifact references. Validate mutations on the server; MCP annotations do not replace authorization checks.

Long operations return an identifier promptly and run asynchronously. Advertise resources for configurations and reports where supported. Add interactive protocol tools using the same runtime action layer after scenario primitives are stable.

### Operations on external targets

An external target may be real equipment. Protocol operations that change state on it (commands, set-points, clock synchronization, counter freeze or reset) are refused by default:

- The target definition lists the allowed operations; anything not listed is refused by the control plane.
- A run that sends a state-changing operation to an external target requires an explicit human confirmation through MCP elicitation when the client supports it. When the client does not support elicitation, the run is refused unless the target definition carries an explicit, reviewed opt-in.
- Reports record each such operation, its confirmation, and how it was confirmed.

## 10. Persistence and reporting

Use SQLite for metadata, run state, and artifact indexes; use files for YAML snapshots, event journals, logs, and reports. Define bounded storage and retention policies.

On controller startup, reconcile stored state with labeled Docker resources. Mark interrupted runs honestly and retain their evidence. Do not resume pending protocol commands automatically. Support inspection and explicit cleanup of recovered test benches.

Generate JSON and JUnit XML reports plus a readable summary. Record each step's expected/observed behavior, durations, errors, relevant event references, cleanup outcome, input hashes, upstream revision, and image digests.

Distinguish assertion failures from infrastructure errors and unsupported tests. Optional packet capture can be added as an evidence artifact without being required for the first milestone.

## 11. Toolchain and dependencies

### Python

- Python 3.12 or later; the exact minimum is fixed in `pyproject.toml` and tested in CI.
- `uv` for environments and the lockfile (`uv.lock`, committed).
- `ruff` (format and lint), `mypy` in strict mode, `pytest` with `pytest-asyncio`.
- A licence check of the locked dependencies in CI against the allowed list of §2.

Runtime dependencies, each pinned in the lockfile:

| Dependency | Purpose | Status |
| --- | --- | --- |
| `mcp` (official MCP Python SDK) | MCP server, stdio and Streamable HTTP | agreed in §3; pin the version at B1 |
| `pydantic` (v2) | typed models, JSON Schema generation | proposed, needs approval |
| `ruamel.yaml` or `PyYAML` (safe loader) | YAML definitions | proposed, choose one at B1 |
| `httpx` | asynchronous HTTP client to the runtimes | proposed, needs approval |
| `docker` (Docker SDK for Python), called through `asyncio.to_thread` | Docker Engine API | proposed, needs approval |

### Rust runtime

- A Cargo workspace under `runtime/`, edition 2021, toolchain pinned in `rust-toolchain.toml`.
- `cargo deny` with the licence policy of §2.
- `#![deny(unsafe_code)]` in the runtime crate; no `unwrap`, `expect`, or panicking indexing on data from the network or the control API.

| Dependency | Purpose | Status |
| --- | --- | --- |
| `igs-iec104` (Git, pinned `rev`) | protocol stack | agreed |
| `tokio` | async runtime (already used upstream) | agreed |
| `axum` | HTTP control API | proposed, needs approval |
| `serde`, `serde_json` | API and event encoding | proposed, needs approval |
| `tracing`, `tracing-subscriber` | structured logs without secrets | proposed, needs approval |

### Containers and CI

- A multi-stage `docker/runtime.Dockerfile`: build with the pinned Rust toolchain and the OpenSSL development files, run on a minimal image with the OpenSSL runtime library, as a non-root user.
- CI on Linux: the standard gate of §2, the Docker integration tests, the image build with its digest as an artifact, and the end-to-end MCP test from milestone 5 onward.

## 12. Suggested repository layout

```text
AGENTS.md
DECISIONS.md
pyproject.toml
uv.lock
src/igs_iec104_mcp/
    server.py
    models/
    definitions/
    docker/
    runtime/
    scenarios/
    storage/
    reporting/
runtime/
    Cargo.toml
    rust-toolchain.toml
    deny.toml
    igs-iec104-runtime/
contracts/
examples/benches/
examples/scenarios/
tests/unit/
tests/integration/
tests/e2e/
docker/
docs/
    ai-log/
```

Keep generated run artifacts outside tracked source directories. Generate or verify published schemas from the typed models to prevent contract drift.

## 13. Delivery milestones and acceptance criteria

| Milestone | Deliverable | Acceptance criterion |
| --- | --- | --- |
| 1. Integration spike | Rust client/server and minimal Python control path | Connect, start transfer, interrogate, update a point, observe it, and stop |
| 2. Contracts and runtime | Validated configuration, versioned API, structured events | Python controls either role and distinguishes acceptance from confirmation |
| 3. Docker orchestration | Test bench creation, readiness, lifecycle, and reconciliation | Two independent test benches coexist; partial failure cleans up owned resources |
| 4. Scenario engine | YAML execution, assertions, deadlines, cancellation | Positive and negative scenarios produce evidence-backed verdicts |
| 5. MCP integration | Definition and execution tools, resources, pagination | A real MCP client creates and executes a test bench scenario |
| 6. External interoperability | External target policy and published server endpoints | Test an external server with a managed client and an external client with a managed server |
| 7. Reports and resilience | JSON/JUnit, recovery, limits, reproducibility manifest | Reports survive cleanup; controller interruption is detected without replaying commands |
| 8. Complete validation release | Scenario catalogue, TLS, redundancy, controlled faults | Documented end-to-end matrix passes against managed and independent implementations |

The first vertical milestone is a Python MCP call that deploys a Docker client/server pair, performs an interrogation, verifies a point, and returns a report. Later milestones extend this working path rather than postponing integration until the end.

Milestones map to the phases of §14: 1 = S, 2 = C, 3 = D, 4 = E, 5 = M, 6 = X, 7 = R, 8 = V.

## 14. Tasks

Each task lists its dependencies, what it produces, and when it is done. The standard gate of §2 applies to every task in addition to its own criterion.

### Phase B — Bootstrap

**B1 — Python project and tooling**
- Depends: none.
- Produce: `pyproject.toml`, `uv.lock`, ruff, mypy strict, pytest, the package skeleton of §12, `AGENTS.md` (summary of §2), `DECISIONS.md`, `docs/ai-log/QUESTIONS.md`, CI running the Python part of the gate.
- Done when: the gate passes on an empty package in CI; the dependency versions of §11 are pinned and approved.

**B2 — Rust runtime skeleton and image**
- Depends: B1.
- Produce: the `runtime/` workspace with `igs-iec104` pinned by Git `rev`, `rust-toolchain.toml`, `deny.toml`, a binary that starts and exits, the Dockerfile of §11, CI building the image.
- Done when: `cargo deny check` passes; the image builds in CI and its digest is published as an artifact; the pinned revision is recorded in `DECISIONS.md`.

### Phase U — Upstream prerequisites (in `igs-iec104`) ⛔

Planned, reviewed and done in the `igs-iec104` repository, with its own tasks and rules; all merged at `e91ecc8` (§4).

- **U1 — Graceful server shutdown.** Used by C3 and D2.
- **U2 — Server connection and transfer events.** Used by C3, E5 and V2.
- **U3 — Ordered client events, offline request policy, and a transmission event.** Used by C2.
- **U4 — Supported action matrix.** Used by C2 and C3.

### Phase S — Integration spike (milestone 1)

**S1 — Runtime, client role**
- Depends: B2.
- Produce: the runtime as a client with a minimal HTTP API: readiness, start transfer, general interrogation, and an event list.
- Done when: against the upstream demo server, the API starts the transfer, runs an interrogation, and returns its answers.

**S2 — Runtime, server role**
- Depends: B2.
- Produce: the runtime as a server with points from its configuration, and an API to update a point.
- Done when: an upstream client (or S1) interrogates it and receives a point updated through the API.

**S3 — Minimal Python control path**
- Depends: S1, S2.
- Produce: a Python function that starts a server and a client runtime as local processes or containers, runs the milestone 1 sequence, and stops them.
- Done when: the milestone 1 criterion passes in an integration test. Record in `docs/ai-log/S3.md` what the spike showed about shutdown, connection observation, and event coverage.

### Phase C — Contracts and runtime (milestone 2)

**C1 — Runtime API and event contract**
- Depends: S3.
- Produce: `contracts/runtime-api.yaml` (OpenAPI) and `contracts/events.schema.json`, versioned; contract tests on both sides.
- Done when: the Python client and the Rust server pass the contract tests.

**C2 — Client role: correlation, generations, point image**
- Depends: C1, U4.
- Produce: the owner task with `OfflineRequests::Discard`, connection generations from the `Connected` events, the binding of requests to generations by `Delivery::Transmitted`, correlation with `answer::judge` within the generation, the verdicts of §7, and the client-side point image.
- Done when: tests cover confirmed, refused (negative and causes 44 to 47), timed out, cancelled, indeterminate (generation ended), and not sent (discarded); a command is refused while the transfer is not started.

**C3 — Server role: points, simulation, command policy, observation**
- Depends: C1, U1, U2, U4.
- Produce: point management, simulation with seeds and periods, the configured command policy, a command log through the handler, the station's connection events (`ServerEvent`) in the runtime's event stream, and the stop through `run_until` with its `ShutdownReport`.
- Done when: tests cover accepted and refused commands, select-before-operate with selection expiry, seeded simulation reproducibility, the connection events of a client that connects, starts and leaves, and a stop that reports its connections; a nonzero drop count of the server events is reported.

**C4 — Definition models and YAML schemas**
- Depends: B1.
- Produce: typed models for test bench, instance, external target, scenario, and run; safe YAML loading; the validations of §5; published JSON Schemas generated from the models.
- Done when: unit tests cover every rejection of §5 with an actionable message; a check fails when the published schemas differ from the models.

**C5 — Definition storage**
- Depends: C4.
- Produce: the project directory, path traversal protection, atomic writes, revision checks, and run snapshots.
- Done when: tests cover traversal attempts, concurrent updates with a stale revision, and a snapshot unaffected by a later edit.

### Phase D — Docker orchestration (milestone 3)

**D1 — Docker adapter and labels**
- Depends: B2, C4.
- Produce: the Docker adapter off the event loop, labeled networks and containers, image digest checks.
- Done when: integration tests create and remove labeled resources and never touch unlabeled ones.

**D2 — Test bench lifecycle**
- Depends: D1, C2, C3.
- Produce: the lifecycle of §6 with readiness, ports bound by the runtimes, graceful stop with a deadline, rollback of partial creation, and evidence preservation.
- Done when: a failure injected at each lifecycle step leaves no owned resource behind; two test benches run side by side.

**D3 — Reconciliation**
- Depends: D2, R1.
- Produce: startup reconciliation of stored state with labeled resources; inspection and explicit cleanup of recovered test benches.
- Done when: a controller killed during a run restarts, marks the run interrupted, keeps its evidence, and does not resume commands.
- Done (D-037): the start marks the unended runs interrupted and sends nothing; `bench_recovered` lists the benches a controller left, and `bench_recovered_remove` removes one with its logs kept in the evidence.

**S4 — Runtime point kinds** (added after D2, D-019)
- Depends: C2, C3.
- Produce: the runtime serves `counter`, `step`, `bitstring`, `packed`, and `normalized_unqualified` points, on the station and in the client's point image, with their time tags where the form has one.
- Split (§2, D-021): S4a `counter` (a station serves it; its update reaches the client as M_IT; the contract is 1.3.0), done; S4b the counter interrogation as a client action (D-022, contract 1.4.0), done; S4c `step` and `bitstring` (D-023, contract 1.5.0), done; S4d `packed` and `normalized_unqualified` (D-024, contract 1.6.0), done. All ten kinds are served.
- Done when: for each kind, a station serves it, a client receives it with its value and quality, and the plan accepts a bench that uses it.

**S5 — Runtime link flags** (added after D2, D-020)
- Depends: S3.
- Produce: the flags `--t0`, `--t1`, `--t2`, `--t3`, `--k`, `--w` on the runtime, checked with the upstream `Parameters` rules, with the defaults of §9.6 when they are absent. The plan stops refusing link parameters.
- Done when: unit tests cover each rule of the flags, and an integration test shows that a short timeout changes the observed behaviour of a link.

### Phase E — Scenario engine (milestone 4)

**E1 — Steps, cursors, and the run state machine**
- Depends: C4, C2.
- Produce: sequential execution, event cursors, per-step and global deadlines, fail-fast.
- Done when: unit tests with a fake runtime cover ordering, deadlines, and stale observations before the cursor.

**E2 — Assertions**
- Depends: E1.
- Produce: value, quality, type, common address, IOA, cause, and timestamp assertions with tolerances.
- Done when: unit tests cover each matcher, its tolerance, and its failure message.
- Done (D-026): the matchers and the two assertion handlers, over a source of events and point images; the
  expected conditions added to the two steps (common address, cause, time tag, quality).

**E2b — Runtime-backed observation source** (added after E2, D-026)
- Depends: E2, S4.
- Produce: the runtime's events carry the decoded observations of each ASDU (contract change), and a source over
  the control client's events and point images, which the scenario engine uses against a real station.
- Done when: an integration test runs an assertion scenario against a station and a client of the runtime.
- Done (D-028): each decoded value is a `point` event after its ASDU (contract 1.7.0), the image point carries
  the common address, and `ControlSource` reads both. The scenario runs against a real station and client.

**E3 — Cancellation and cleanup**
- Depends: E1.
- Produce: cancellation of a run, and cleanup steps after success, failure, or cancellation.
- Done when: a cancelled run reports `cancelled`, runs its cleanup, and leaves no pending command.
- Done (D-027): the cancel event, the CANCELLED outcome and status, the cleanup steps after every outcome, each
  with a deadline. `execution_seconds` is not enforced here (it belongs to the run controller, M2 and D3), and
  the "no pending command" check is the handler contract until command actions exist.

**E4 — Parallel branches and barriers**
- Depends: E1.
- Done when: tests cover a barrier reached, a branch failure with fail-fast on and off, and a deadline during a barrier.
- Done (D-029): the `parallel` step is the barrier; `timeout` bounds it; branch steps have labelled verdicts.

**E5 — Restart and reconnect primitives**
- Depends: D2, E1, U2.
- Done when: an end-to-end scenario restarts the server and asserts that the client reconnects, starts the transfer, and interrogates again.
- Done (D-030): the instance actions over a local bench (station and client as processes). The restart of a container of the managed (Docker) bench is not in E5: it needs a fixed station port in the plan.

### Phase M — MCP integration (milestone 5)

**M1 — MCP server over stdio**
- Depends: B1, C4.
- Produce: the server with stdout reserved for MCP, stable error codes, and the definition tools of §9.
- Done when: an MCP client test lists the tools and validates a definition.
- Done (D-031): the nine definition tools over stdio, with stable error codes; a real MCP client lists them and validates a bench.

**M2 — Scenario and run tools**
- Depends: M1, E3, D2.
- Produce: `scenario_*`, `scenario_run`, `run_*` with asynchronous runs, pagination, and resources for configurations and reports.
- Done when: the milestone 5 criterion passes with a real MCP client in CI.
- Done (D-033): the run tools run a scenario on a Docker bench, and `tests/integration/test_mcp_run.py` passes locally on Podman; the CI run confirms it on Docker. Resources for configurations and reports are not in M2 (reports come with R2).

**M3 — Confirmation of operations on external targets**
- Depends: M2, X1.
- Produce: the policy of §9 (allowed operations, elicitation, recorded confirmation).
- Done when: tests show a state-changing operation refused when not allowed, refused without confirmation, and recorded when confirmed.
- Done (D-038): a command to an external target is confirmed by a person through elicitation, or by a reviewed opt-in when the client cannot be asked, and each decision is journaled. External connections are still refused by the planner, so a confirmed command is unsupported.

**M4 — Streamable HTTP transport** ⛔
- Depends: M2.
- Produce: the HTTP transport with authentication and access control.
- Done when: a maintainer has reviewed the authentication design and the exposure.

### Phase X — External interoperability (milestone 6)

**X1 — External target model and policy**
- Depends: C4, C2.
- Done when: the control plane refuses undeclared operations on a target, and a connectivity probe is explicit and opt-in.
- Done (D-034): `undeclared` refuses a scenario's operation a target does not list, `target_validate` reports a target without connecting, and its probe is explicit (`probe`) and sends no IEC frame.

**X2 — Published server endpoints**
- Depends: D2.
- Done when: a client outside Docker reaches a managed server at the advertised endpoint on Linux and on Docker Desktop/WSL.
- Done (D-036): the Linux check passes on Podman (WSL2 host). The Docker Desktop and WSL check is an open item of the platform matrix (V4), as the user decided.

**X3 — Interoperability against an independent implementation** ⛔
- Depends: X1, X2, M2.
- Produce: scenarios against at least one independent implementation, run as a black box (§2).
- Done when: a maintainer reviews the results and the feature matrix.

### Phase R — Reports and resilience (milestone 7)

- **R1 — Run store** (SQLite metadata, event journals, artifact index). Depends: C5.
  Done (D-032): the store passes its tests (the lifecycle and transitions, a bounded journal, artifacts written once, pages, and a reopen).
- **R2 — Reports** (JSON, JUnit XML, summary with the manifest of §10). Depends: R1, E2.
  Done (D-039): `run_report` makes the JSON, the JUnit XML, and the summary of an ended run from one document, and keeps each as an artifact of the run, so it outlives the bench.
- **R3 — Limits and retention** (event buffers, storage bounds, retention policy). Depends: R1, D2.
  Done (D-040): a step whose events were dropped by a runtime's log is an error, a drop count is a gap in the run and its report, and `run_prune` removes the ended runs a retention policy selects, never a run whose bench is still there.

Each is done when its tests pass and its reports survive test bench cleanup.

### Phase V — Validation release (milestone 8)

- **V1 — TLS scenarios.** Depends: C2, C3, D2. Both roles with the upstream TLS settings; acceptance and rejection cases.
  Split (D-041): V1a, the runtime secures both roles with the upstream TLS settings, is done (D-041). V1b, the controller delivers the certificate material through the archive interface and the bench definition names it, is done (D-042). V1c, the acceptance and rejection scenarios on Docker, is done (D-043). V1 is done.
- **V2 — Redundancy scenarios.** Depends: E5, U2.
  Split (D-044): V2a, the runtime's client role as a redundancy group, is done (D-044). V2b, the bench model and the planner accept one client with several stations, is done (D-045). V2c, a managed station's stop and the switchover scenarios on Docker, is done (D-046). V2 is done.
- **V3 — Controlled faults** ⛔ (interruption and latency through a dedicated proxy). Depends: D2. A maintainer approves the component and its privileges.
- **V4 — Release documentation and platform matrix.** Depends: all previous tasks.
  Started before its dependencies, as the user decided (D-047). The documentation part is done (D-047). The platform matrix remains: it is complete when M4, X3 and V3 are done and the Docker Desktop and WSL check has run.
- **V5 — Release** ⛔. A maintainer reviews the matrix, the limitations, and the licences.

## 15. Test strategy and release definition

Use unit tests for schema validation, assertion matching, response correlation, deadlines, cancellation, and report generation. Use Docker integration tests for networking, readiness, rollback, simultaneous test benches, and cleanup. Use end-to-end tests through an actual MCP client.

Cover interrogation, reads, spontaneous updates, accepted/refused commands, selection expiry, reconnect, redundancy, TLS acceptance/rejection, event-buffer overflow, and interrupted executions. Validate timeout and indeterminate results as well as success.

Test external interoperability against at least one independent IEC 104 implementation, used as a black box (§2). Tests where both endpoints use igs-iec104 verify the test bench but cannot alone establish interoperability or protocol conformance. State the supported feature matrix and observed limitations; do not describe these reports as certification.

Integration tests must not depend on fixed host ports or on picking a free port before a process binds it; let processes bind and report their ports (§6).

Release documentation must include setup, Docker connectivity, external endpoint configuration, YAML schemas, examples, tool contracts, artifact retention, troubleshooting, and the tested platform matrix.

## 16. Progress

| Task | Done |
| --- | --- |
| B1 Python project and tooling | [x] |
| B2 Rust runtime skeleton and image | [x] |
| U1 Graceful server shutdown (upstream) ⛔ | [x] |
| U2 Server connection events (upstream) ⛔ | [x] |
| U3 Client command policy on disconnect (upstream) ⛔ | [x] |
| U4 Supported action matrix (upstream) ⛔ | [x] |
| S1 Runtime, client role | [x] |
| S2 Runtime, server role | [x] |
| S3 Minimal Python control path | [x] |
| C1 Runtime API and event contract | [x] |
| C2 Client correlation, generations, point image | [x] |
| C3 Server points, simulation, command policy | [x] |
| C4 Definition models and YAML schemas | [x] |
| C5 Definition storage | [x] |
| D1 Docker adapter and labels | [x] |
| D2 Test bench lifecycle | [x] |
| S4 Runtime point kinds | [x] |
| S5 Runtime link flags | [x] |
| D3 Reconciliation | [x] |
| E1 Steps, cursors, run state machine | [x] |
| E2 Assertions | [x] |
| E2b Runtime-backed observation source | [x] |
| E3 Cancellation and cleanup | [x] |
| E4 Parallel branches and barriers | [x] |
| E5 Restart and reconnect primitives | [x] |
| M1 MCP server over stdio | [x] |
| M2 Scenario and run tools | [x] |
| M3 Confirmation on external targets | [x] |
| M4 Streamable HTTP transport ⛔ | [ ] |
| X1 External target model and policy | [x] |
| X2 Published server endpoints | [x] |
| X3 Interoperability, independent implementation ⛔ | [ ] |
| R1 Run store | [x] |
| R2 Reports | [x] |
| R3 Limits and retention | [x] |
| V1 TLS scenarios | [x] |
| V2 Redundancy scenarios | [x] |
| V3 Controlled faults ⛔ | [ ] |
| V4 Release documentation and platform matrix | [ ] |
| V5 Release ⛔ | [ ] |

## 17. References

- Library and status: https://github.com/aklira/igs-iec104
- Demo server capabilities and limitations: https://github.com/aklira/igs-iec104/blob/main/docs/igs104-server.md
- Client request/event semantics and the owner pattern: https://github.com/aklira/igs-iec104/blob/main/crates/igs-iec104/src/client.rs
- Answer correlation: https://github.com/aklira/igs-iec104/blob/main/crates/igs-iec104/src/answer.rs
- Supported actions of the library: https://github.com/aklira/igs-iec104/blob/main/docs/capabilities.md
- Server API: https://github.com/aklira/igs-iec104/blob/main/crates/igs-iec104/src/server.rs
- Upstream decisions (D-021 and others): https://github.com/aklira/igs-iec104/blob/main/PROVENANCE.md
- Official MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- MCP transport specification: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- Docker networking: https://docs.docker.com/engine/network/
