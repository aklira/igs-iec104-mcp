# Decisions

Each decision has an ID (`D-001`, ...), the date, the reasons, and the test that shows it, when a
test can show it.

## D-001: Python toolchain

- Date: 2026-10-11 (task B1).
- Decision: `uv` manages the environment and the lockfile (`uv.lock`, committed). The minimum
  Python is 3.12 (`requires-python = ">=3.12"`), and `.python-version` pins 3.12 for development and
  CI. The project is not packaged (`[tool.uv] package = false`): no build backend is needed yet,
  and adding one would add a dependency that §11 does not list. Packaging is decided when the
  project is distributed.
- Test: the gate of `AGENTS.md` runs with `uv sync --locked`, so the lockfile must match
  `pyproject.toml`.

## D-002: YAML library

- Date: 2026-10-11 (task B1).
- Decision: `ruamel.yaml` (MIT) reads definitions with its safe loader (`YAML(typ="safe")`).
- Reason: §11 allowed either `ruamel.yaml` or `PyYAML`. `ruamel.yaml` ships type information
  (`py.typed`), so mypy in strict mode needs no stub package. `PyYAML` has no type information, so
  it would need `types-PyYAML`, which §11 does not list.
- Test: `tests/unit/test_yaml_policy.py` checks that the safe loader refuses a tag that constructs a
  Python object.

## D-003: Lint and type-check settings

- Date: 2026-10-11 (task B1).
- Decision: ruff checks the rule families E, F, W, I, B, UP, SIM and RUF with a line length of 88.
  mypy runs in strict mode over `src`, `tests` and `scripts`. pytest collects `tests/unit` with
  `asyncio_mode = "auto"`.

## D-004: Upstream revision of `igs-iec104`

- Date: 2026-10-11 (task B2).
- Decision: the runtime depends on `igs-iec104` at revision `e91ecc824fcbc750262a5c8505c8812457d200c1`
  (commit `e91ecc8`), pinned by `rev` in `runtime/igs-iec104-runtime/Cargo.toml`. Cargo resolves the
  codec and link crates of that repository from the same revision.
- Reason: `e91ecc8` is the first revision with the upstream prerequisites U1 to U4 of
  `IMPLEMENTATION.md` §4. Its upstream CI run succeeded. The upstream repository is public, so CI
  and the image build fetch it without credentials.
- Change: a later revision is a new decision in this file, with the same test.
- Test: `tests/unit/test_runtime_pin.py` checks that the `rev` of the manifest is the one recorded
  here.

## D-005: Rust toolchain and base images

- Date: 2026-10-11 (task B2).
- Decision: `runtime/rust-toolchain.toml` pins the toolchain `1.99.0`. The minimum supported Rust
  version stays `1.85` (`rust-version` in `runtime/Cargo.toml`, the minimum of `IMPLEMENTATION.md`
  §11). The image builds on `rust:1.99.0-trixie` and runs on `debian:trixie-slim`.
- Open: the base images are pinned by tag, not by digest. The digests are recorded when CI
  publishes the image.
- Test: `tests/unit/test_runtime_pin.py` checks that the channel of the toolchain file is the one
  recorded here.

## D-006: TLS backend of the runtime

- Date: 2026-10-11 (task B2).
- Decision: the runtime uses the default TLS backend of `igs-iec104`, OpenSSL (its `openssl`
  feature), and not `rustls`.
- Reason: the upstream documentation says the `rustls` backend lacks mandatory items of the
  IEC 62351-3 profile (CBC, DHE and CCM cipher suites, RSA-PSS, and TLS 1.2 renegotiation). The
  build needs the OpenSSL development files (`libssl-dev`), and the image runs with `libssl3t64`.
- Current state: the binary does not call OpenSSL yet, so the linker drops `libssl` from it
  (checked with `ldd` in the image, built with Podman). The runtime image installs `libssl3t64`
  already, as §11 requires; the library becomes needed when S1 and S2 use TLS.
- Test: none in B2.

## D-007: Licences admitted for transitive and development dependencies

- Date: 2026-10-11 (task B1 finding, decided by the maintainer for B1 and B2).
- Decision: in addition to MIT, Apache-2.0, BSD, and ISC, the policy of `IMPLEMENTATION.md` §2
  admits MPL-2.0, PSF-2.0, and MIT-0. They are admitted only for transitive and development
  dependencies that are used unmodified. A direct dependency under one of these licences still needs
  approval.
- Reason: four installed distributions that approved dependencies pull in use these licences:
  `certifi` (MPL-2.0, through `httpx`), `typing_extensions` (PSF-2.0, through `anyio`, `httpx`,
  and `pydantic`), `cffi` (MIT-0, through `cryptography` and `mcp[crypto]`), and `pathspec`
  (MPL-2.0, through `mypy`, development only). Removing them would mean removing approved
  dependencies.
- Enforcement: `scripts/check_licences.py` checks the installed distributions, and CI runs it.
  `tests/unit/test_licence_policy.py` checks the admitted and refused expressions.
- Scope: the Rust runtime keeps the list of its `deny.toml`, which already matches §2.

## D-008: Runtime processes are local processes, and report their bound addresses

- Date: 2026-10-11 (task S3).
- Decision: the controller starts runtimes as local processes (`asyncio` subprocesses). A runtime
  binds port 0 when no port is imposed, and prints its bound addresses once on standard output, as one
  JSON line: `{"control_address": ..., "station_address": ...}`, with `station_address` null for a
  client. Standard error carries the log. Containers come with the Docker adapter (D1).
- Reason: a controller that gave port 0 cannot learn the port otherwise, and the plan forbids picking a
  free port on the host beforehand (§6). Parsing the log would couple the controller to a message.
- Stop: SIGTERM, then a kill after 20 seconds. The station drains its connections for at most five
  seconds (U1).
- Test: `tests/integration/test_milestone1.py` starts both runtimes this way and runs the milestone
  sequence. `tests/unit/test_asdu.py` checks the reader of the answers.

## D-009: Control API contract, control token, and validators

- Date: 2026-10-11 (task C1).
- Decision: the contract is `contracts/runtime-api.yaml` (OpenAPI 3.1) and `contracts/events.schema.json`
  (JSON Schema 2020-12), both at version 1.0.0. The runtime reports the version on readiness as
  `contract`. Every route of the control API, including the unknown paths, needs the control token:
  the runtime reads it from the file given by `--control-token-file` (required), and expects
  `Authorization: Bearer <token>`. The token is at least 32 letters, digits, `-` or `_`; its `Debug`
  form is redacted, and it is never logged. The controller writes a new token per run, with
  `secrets.token_urlsafe(32)`, to a file readable by its owner only (mode 0600).
- Reason: §3 requires a credential per test bench, kept out of logs and reports. A file keeps the
  token out of the process arguments and the environment. The contract is versioned so that an
  incompatible change is visible in both files.
- Validators: `jsonschema` and `openapi-spec-validator` (both approved for C1) are development
  dependencies, used by the tests. `jsonschema` ships no type stubs, and `types-jsonschema` is not a
  listed dependency: mypy ignores missing imports for `jsonschema` (pyproject, tests only).
- Tests: `tests/unit/test_contract_documents.py` (both documents are valid, and they name the same
  version); `tests/integration/test_contract.py` (each response of a station and a client has a
  documented status and a body that matches its schema, and the requests without the token are
  refused with 401); `runtime/igs-iec104-runtime/tests/contract.rs` (every event kind matches the
  schema, and the schema names exactly the kinds the client records); and
  `runtime/igs-iec104-runtime/tests/server_role.rs` (no token, or a wrong one, gives 401 on every path).

## D-010: Decoded values of the ASDUs come with the point image (C2)

- Date: 2026-10-11 (task C1).
- Decision: the contract gives each ASDU as its header (type identification, cause, P/N bit, common
  address) and its hexadecimal encoding. The decoded values are not in version 1.0.0. They come with
  the point image of task C2, as an addition that keeps the version compatible.
- Reason: the decoded form depends on the point image (values, quality, time tags, and the point
  types of the station), which C2 builds. Describing it now would fix a shape before the code that
  produces it exists.

## D-011: Client requests, verdicts, and the point image (contract 1.1.0)

- Date: 2026-10-11 (task C2).
- Decision: the client correlates its requests with the station's answers. A request (an
  interrogation, or a single command) has a stage (`queued`, `transmitted`, `activated`, `ended`), and
  an outcome when it ends: `confirmed`, `refused` (with the cause and the P/N bit), `timed_out`,
  `cancelled`, `indeterminate`, or `not_sent`. A select is confirmed by its activation confirmation; an
  execution, and an interrogation, by their activation termination. An answer is judged only against a
  request written in the current connection generation. A request whose connection ended after it was
  written is `indeterminate`, and is never sent again. A command is refused unless the data transfer is
  started. Two open requests cannot share a type identification and an address, and at most eight are
  open at a time, below the client's channel capacity of 32, so the owner never waits for room in the
  command channel.
- Contract 1.1.0 (additive): every event carries its `generation`; a `verdict` event reports an outcome;
  new routes are `POST /v1/commands/single`, `GET` and `POST .../cancel` for a request record, and
  `GET /v1/image/points`. The interrogation and command answers carry the identity of the request.
- Point image: the last value of each point the station sent, with its quality, time tag (read as
  2000 plus the two-digit CP56 year), cause, observation time, and generation. It covers the single,
  double, and measured types, plain and time-tagged, as the profile defines them.
- Test: `runtime/igs-iec104-runtime/src/correlation.rs` (rules, with an injected clock),
  `src/image.rs` (decoding), and `tests/client_correlation.rs` (against the upstream station and a silent
  peer). `not_sent` is reached end to end only through a race the test cannot force; its rule is tested
  in `correlation`.

## D-012: Station commands, simulation, station events, and the stop report (contract 1.2.0)

- Date: 2026-10-11 (task C3).
- Decision: each point of the station has a command policy (`unknown_address` by default, `refuse`, or
  `accept` for a single or double point). A command is judged by the policy of its point; an accepted
  command is confirmed, and the point keeps its value (the controller updates it). Each judgement is
  queued without waiting, and recorded in the station's event log. A point may change by itself
  (`simulation`): toggle, ramp, or a seeded walk, each step a function of the step number and the seed,
  published at the period. The station reports its connections on `GET /v1/station/events`, with the
  events the library reports (connected, transfer, superseded, closed with its end), and the commands,
  and it reports the events it lost. The selection time-out is a setting of the station, 30 seconds by
  default. The stop returns the library's report: the connections open at the stop, those closed by force,
  and the events still held.
- Contract 1.2.0 (additive): `contracts/station-events.schema.json` and `GET /v1/station/events`.
  Connection identities start at zero, as the library numbers them.
- Test: `src/simulate.rs` (the sequences, the wrap-around, the seed, and a driver test with a paused
  clock), `src/points.rs` (the validation of the policy and the simulation), `src/station_log.rs`, and
  `tests/station.rs` (the policy and the log, the selection time-out, the connection events, the stop
  report, and the losses), with `tests/contract.rs` for the station events.

## D-013: Definition models, YAML reading, and published schemas

- Date: 2026-10-11 (task C4).
- Decision: the definitions (test bench, scenario, run) are pydantic models, with `extra="forbid"` on
  every model, so that an unknown field is refused by name. Each top-level definition has a
  `schema_version` of 1. YAML is read with the safe loader of `ruamel.yaml`, with duplicate keys refused.
  The JSON Schemas are generated from the models into `contracts/definitions/`, and `--check` (also a unit
  test) fails when a committed schema differs from its model.
- Rules: the common address is 1 to 65534; an information object address is 1 to 16777215; the link
  parameters follow the upstream rules (t0 to t2 are whole seconds from 1 to 255, t2 below t1, t3 a whole
  number of seconds of at least 1, k and w from 1 to 32767); a timeout is above zero and at most an hour
  for a step, and a day for a whole scenario; a secret is named as `secret:<name>` and key material is
  never written in a definition; a scenario action outside the list is refused with the list.
- Point kinds: the ten monitoring kinds of the upstream `PointValue` are accepted, with the forms
  of their values; the runtime serves five of them today (see Q-002).
- The plan's example (§8) writes `type:` for a point; the definitions write `kind:`, as the point model does.
- Test: `tests/unit/test_models_*.py` (each rejection of §5, with its message), and
  `tests/unit/test_models_schemas.py` (the committed schemas match the models; a changed schema is reported).

## D-014: Project directory, revisions, and run snapshots

- Date: 2026-10-11 (task C5).
- Decision: the definitions live in one explicit project directory: `benches/<name>.yaml`,
  `scenarios/<name>.yaml`, and `runs/<run-id>/snapshot.json`. The directory must exist; the store does
  not create it. A definition's file name is its `name`, and a file whose definition names another is
  refused.
- Containment: a name is checked against the name pattern before it becomes a path, and a path is
  resolved, with its symbolic links followed, before it is read or written. A path outside the project
  directory is refused. The lock file is opened without following a link.
- Revisions: a file's revision is the SHA-256 of its bytes. An update names the revision it was read at;
  it is refused when the file has changed or was removed since. A new definition is written only when no
  file of that name exists.
- Atomic writes: the text is written to a temporary file in the same directory, flushed to disk, and
  replaced in one step; the temporary file is removed when the write fails. Writers hold an exclusive
  `flock` on `.lock` in the project directory, so the check and the replacement are one step, also
  between processes. The lock uses the standard library only (no dependency).
- Validation: a definition is checked before it is written, so an invalid one never reaches the project.
- Snapshots: a run's snapshot holds the bench and the scenario as they were at the start, checked against
  each other with `check_scenario`, and their SHA-256 hashes. It is written once and never changed; it is
  read back with its hashes checked.
- Test: `tests/unit/test_definitions_store.py` (traversal, links, stale and concurrent updates, failed
  writes, invalid definitions, the snapshot unaffected by a later edit, a changed snapshot refused).

## D-015: Docker resources, labels, hardening, and image digests

- Date: 2026-10-11 (task D1).
- Decision: every network and container the controller creates carries four labels in the namespace
  `dev.igs-iec104.mcp`: `managed=true`, `project`, `bench`, and `run`; a container also carries `instance`.
  A resource belongs to an owner only when its labels match exactly, and every lookup, removal, and rollback
  checks the labels again: a resource found by name alone is refused, never removed.
- Names: a Docker name is `igsmcp-<first 12 hex digits of the run>-<part>`, where the part is the bench's
  network name or the instance name. The name is checked against the name pattern and at most 63
  characters. Ownership is decided by the labels, not by the name.
- Hardening: a container spec has no field for a volume, a bind, a port, a privilege, the host network, or an
  extra capability. Every container is created unprivileged, with all capabilities dropped and
  `no-new-privileges` (its root filesystem is writable: see D-016). Its memory, CPUs, and processes are bounded by `Limits`.
- Images: a container is created from the image identity (`sha256:...`), never from a tag. A repository
  digest is resolved on the engine and checked against the image's repo digests; a tag is refused before any
  call.
- Idempotence and rollback: creating a resource that exists for the same owner finds it and creates nothing.
  A creation block removes the resources it created, in reverse order, when it fails; a resource it found
  already existing is kept. A failed removal is reported by name.
- Threads: the Docker SDK is synchronous, so every call runs through `asyncio.to_thread`.
- Dependency: the Docker SDK for Python (`docker`), approved at B1 (§11). The SDK ships no type hints, so mypy
  treats its names as `Any` (`pyproject.toml`), and one import carries a typed ignore.
- Tests: `tests/unit/test_docker_specs.py` (owner, labels, names, digests, the absence of mount and privilege
  fields, limits); `tests/integration/test_docker_adapter.py` (against a real engine: create, list, remove;
  hardening read back from the engine; an unlabelled resource with the same name is refused and kept; another
  run's resources are kept; a failed creation is rolled back; the digest checks). The integration tests pull
  one small image by its digest and fail in CI without an engine.

## D-016: The test bench lifecycle

- Date: 2026-10-11 (task D2).
- Decision: a bench starts in this order: the networks, each station (create, copy its files, start, ready),
  then each client (create, copy its files, start, ready, connected). The control API of each runtime listens
  on port 8080 inside its container, and the engine publishes it on 127.0.0.1 at a port it picks; the
  controller reads that port back. A station listens on port 0 and reports the port it bound. A client is
  given its station's address on their network (the container address the engine reports, and the bound port);
  the runtime takes IP addresses, not container names.
- Files: the control token and the points are copied into the container with the engine's archive
  interface, into the directory `/run/igs`, which the image creates, owned by the runtime's user (UID 10001,
  fixed in `docker/runtime.Dockerfile`). They are read by that user alone (mode 0400). They are never in the
  environment, a bind mount, or the evidence. The files are copied before the container starts, so they
  live in the container's writable layer until the container is removed. The controller writes the token to
  a temporary directory outside the project, which is removed when the start ends.
- Correction: the first version mounted an in-memory directory at `/run/igs`. A mount made at start hides
  files copied before it, and Docker creates the mount point only at start, so the copy failed there; Podman
  accepted both, which is why local runs passed. The directory is now created by the image.
- Plan: a bench is planned before anything is created. The plan refuses, by name: a point kind the runtime
  does not serve (Q-002); link parameters other than the defaults (Q-003); a connection that reaches an
  external target (task X1); a connection whose ends are not on one network; a client without exactly one
  connection. Every problem is reported at once.
- Evidence: a manifest (the bench, the run, the requested image, its resolved identity, the networks and the
  runtimes) is written at the start. Each runtime's logs and record are kept when it stops, and the logs and
  record of each container of a failed start are kept before the rollback. `stop.json` records each exit code
  and whether the stop was graceful (status 0 after SIGTERM).
- Rollback: a failure at any step removes the resources the start created, through the creation block of D1.
  The checkpoint of each step is a test hook; the test injects a failure at every step.
- Not in D2: the cleanup policy (`CleanupPolicy`) and the execution time limit (`execution_seconds`) are read
  by the caller (the scenario engine, E3); external connectivity and published station ports come with X1.
- Tests: `tests/unit/test_docker_plan.py` (each refusal and the plan of the basic pair);
  `tests/integration/test_docker_bench.py` (the basic pair comes up, stops gracefully, and keeps its evidence
  without the token; a failure at each step leaves no owned resource; two benches run side by side; a plan
  that does not run creates nothing). The integration tests need the runtime image, named by
  `IGS_RUNTIME_IMAGE`; the CI integration job builds it.
- Correction: the earlier records (C4, D-013, Q-002) said eleven point kinds. The upstream `PointValue` has
  ten variants; the unserved kinds are step, bitstring, counter, packed, and normalized-unqualified.

## D-017: The scenario engine: steps, cursors, deadlines, and the run states

- Date: 2026-10-11 (task E1).
- Decision: the engine runs the steps of a scenario in order. It knows no protocol: each action is run by a
  handler that the caller supplies, and a handler reads the runtimes' events through a `Source` (the last
  sequence number of an instance, and the events after a cursor). An action with no handler is UNSUPPORTED,
  never passed.
- Cursors: a handler captures the cursor of its instance before it triggers or waits for anything. The engine
  matches an event only when its sequence number is after the cursor, even when the source returns older
  events too.
- Deadlines: a step with a `timeout` has its deadline (asyncio's monotonic clock); a step without one is bounded
  by the scenario only. The scenario's `global_timeout`, when set, bounds the whole run. A deadline that passes
  during a step gives that step TIMED_OUT; the global one ends the run as FAILED.
- Fail-fast: `fail_fast` (true by default in the model) stops the run after the first step whose verdict is
  not PASSED. Without it, the later steps run and the run is judged on all of them.
- Status of a run: any ERROR or UNSUPPORTED verdict makes the run ERROR (it could not be judged); otherwise any
  verdict that is not PASSED, or the global deadline, makes it FAILED; otherwise it is PASSED. A run moves
  only along its states (pending, running, then an ended status), and `RunMachine` refuses the others.
- Not in E1: the handlers of the actions (E2 for the assertions, E5 for the restarts), the cleanup steps after
  success, failure, or cancellation (E3), parallel branches (E4), and the `Source` over the runtime's events
  API, which E2 binds to the control client. Steps are numbered in their verdicts; the schema has no step names.
- Test: `tests/unit/test_scenario_engine.py` (ordering, a stale observation before the cursor, a step deadline,
  the global deadline during a step, fail-fast on and off, an action with no handler, an unexpected error, the
  run states, a step without a deadline).
- Correction 2: the root filesystem of a runtime is writable. The engine refuses an archive copy into a
  container whose root is read-only ("container rootfs is marked read-only", seen in the CI run of 72daafc),
  while Podman accepted it, so the files of a run need a writable container. The other hardening stays: the
  runtime's non-root user, no privilege, all capabilities dropped, `no-new-privileges`, the resource limits,
  and no bind, volume, or socket. A read-only root with the files in a volume is a later option, to check on
  Docker before it is used.

## D-018: Reaching the runtimes from the host through relays

- Date: 2026-10-11 (task D2, after the CI run of 72daafc).
- Problem: the engine does not publish a port of a container that is on an internal network (the bench
  network, D-015). The first design published each runtime's control port there, so the controller could not
  reach any runtime. A bench network must stay internal (§6), so the publication moves elsewhere.
- Decision: each runtime keeps its bench network, which is internal, and publishes no port. For each runtime,
  a relay container (`docker/relay.py`, image `docker/relay.Dockerfile`) is created on the bench's control
  network, which is not internal and is the only network the host reaches. The relay publishes the control
  port on 127.0.0.1 at a port the engine picks, and forwards every connection, byte for byte, to the runtime's
  control port on its bench network. The relay is then joined to the runtime's bench network. Only relays are
  on the control network.
- Order: the runtime starts first, so its address on its network is known; then the relay is created with that
  address as its target; then the relay is started and read back; then the runtime is polled through it.
- Limits and privilege: the relay holds no protocol logic and no token. It is a Python script, with the
  standard library only, under the same restrictions as a runtime (non-root user 10002, no privilege, all
  capabilities dropped, `no-new-privileges`) and small limits (0.25 CPU, 64 MiB, 32 processes). A relay can
  reach the outside through the control network: that is the egress this design accepts. Restricting it is
  left for the external-connectivity work (X1), where the plan asks for egress restrictions.
- The name `control` is reserved for the control network; a bench that names a network so is refused by the
  plan.
- Corrections to D-016: the runtime's control port is no longer published by the runtime; the controller reads
  the relay's published port. The runtime's root filesystem is writable (see D-015).
- Tests: `tests/unit/test_relay.py` (the address parsing and the forwarding of bytes both ways, and a refused
  target closes the client); `tests/integration/test_docker_bench.py` (a runtime publishes no port; the relay
  does; the stop keeps the relay's logs). The integration tests need `IGS_RELAY_IMAGE` as well as
  `IGS_RUNTIME_IMAGE`; the CI integration job builds both.

## D-019: The point kinds of the definitions (closes Q-002)

- Date: 2026-10-11.
- Decision: the definitions accept the ten monitoring kinds of the upstream `PointValue`, as §5 asks. A bench
  that uses a kind the runtime does not serve yet is refused by the plan, by name, before anything is created
  (`docker/plan.py`). The runtime serves the rest in task S4: `counter` first (§7 names counter interrogation),
  then `step`, `bitstring`, `packed`, and `normalized_unqualified`.
- Why: the schema stays stable; a later runtime change does not change a definition; a bench that cannot run
  fails at the plan with a clear message.

## D-020: The link parameters of the runtimes (closes Q-003)

- Date: 2026-10-11.
- Decision: the runtime takes flags for the link parameters (`--t0`, `--t1`, `--t2`, `--t3`, `--k`, `--w`), in
  the task S5. Each flag is checked with the upstream `Parameters` rules, and the defaults of §9.6 are kept when
  a flag is absent. The API contract does not change: the flags are command-line arguments only. The plan's
  refusal of non-default parameters (D-016) stays until S5 is done, and is then removed.
- Why: a bench sets the parameters in its definition, and a silent default would be wrong; the timeout and
  reconnection scenarios (E5) need the parameters.

## D-021: The counter point kind (S4a) and the split of S4

- Date: 2026-10-11 (task S4a).
- Decision: the runtime serves `counter` points (M_IT_NA_1, or M_IT_TB_1 when stamped). The value is an integer
  from -2147483648 to 2147483647; the reading's sequence number is 0 when the station serves it. The client
  decodes a counter reading into its point image, with the invalid flag as its quality; the carry and adjusted
  flags stay with the value. A counter cannot be simulated by a ramp or a walk, since those need a measured
  point. An update of a counter point leaves as a spontaneous M_IT (cause 3).
- Contract: a new point kind is an additive change, so the contract is 1.3.0 (`runtime-api.yaml`, the two
  event schemas, and `CONTRACT_VERSION`).
- Split of S4: S4a is the counter's station value and its client reception through an update; S4b is the
  counter interrogation as a client action (the upstream station already answers it); S4c is `step` and
  `bitstring`; S4d is `packed` and `normalized_unqualified`. Each part is a commit of its own.
- Tests: `points.rs` (the value's range and form, and the ramp refusal), `image.rs` (the decoding of plain
  and invalid counter readings), `tests/station.rs` (a counter point is served, and its update reaches a
  client as a spontaneous counter reading).

## D-022: The counter interrogation as a client action (S4b)

- Date: 2026-10-11 (task S4b).
- Decision: the control API asks a counter interrogation (C_CI_NA_1) with `POST /v1/interrogation/counter`,
  body `{"group": 0}` (0 for every counter, the general request; 1 to 4 for a group). Any other group is refused
  with 400, before anything is sent. The request is correlated like the general interrogation: it is confirmed
  by its activation confirmation and completes with the activation termination (cause 10). The counter readings
  that come back (M_IT, causes 37 to 41) go to the point image. Freezing and resetting are not requests of the
  control API.
- Contract: a new route is an additive change, so the contract is 1.4.0 (`runtime-api.yaml`, the two event
  schemas, and `CONTRACT_VERSION`). The request kind `counter_interrogation` is added to the kinds of a request
  record.
- Python: `ControlClient.counter_interrogation(group)` posts the same body.
- Tests: `correlation.rs` (a counter interrogation completes with its termination), and `tests/client_role.rs`
  (the invalid group is refused with 400; a counter interrogation is accepted; the station's counter reading
  (M_IT, cause 37) reaches the image with its value).

## D-023: The step and bitstring point kinds (S4c)

- Date: 2026-10-11 (task S4c).
- Decision: the runtime serves `step` points (M_ST_NA_1, or M_ST_TB_1 when stamped), whose value is an object
  `{"value": -64..63, "transient": true|false}`, and `bitstring` points (M_BO_NA_1, or M_BO_TB_1 when stamped),
  whose value is an integer from 0 to 4294967295. In the bitstring, bit n (1 to 32) is the bit of 2^(n - 1): the
  number is read and written through the bit numbers of the upstream `Bsi`, so the number is the one the peer
  sees. The client decodes both into its point image, with the quality of the value. Neither can be simulated:
  a walk or a ramp needs a measured point, and a toggle a single or double point.
- Contract: a new point kind is additive, so the contract is 1.5.0.
- The upstream `Bsi` has no constructor from a number: the runtime builds it bit by bit (`with_bit`) and reads it
  back (`bit`), so the mapping rests on the upstream bit numbering alone.
- Tests: `points.rs` (the forms, the round trip of step and bitstring, the refusals of the forms, the refusal of a
  simulation), `image.rs` (the decoding of plain and time-tagged step and bitstring readings), and
  `tests/station.rs` (a step and a bitstring are served, and their updates reach a client as M_ST and M_BO
  readings with their values).

## D-024: The packed and normalized-unqualified point kinds (S4d): every kind is served

- Date: 2026-10-11 (task S4d).
- Decision: the runtime serves `packed` points (M_PS_NA_1, which has no time-tagged form), whose value is an
  object `{"status": 0..65535, "change": 0..65535}`, where bit n (1 to 16) is point n of the status and of the
  change, as the upstream `Scd` numbers them; and `normalized_unqualified` points (M_ME_ND_1, no time tag), whose
  value is a normalized number as for `normalized`. The client decodes a packed reading with its quality, and an
  unqualified reading with no quality flag set, since that type carries no quality descriptor. Neither can be
  simulated (a walk or a ramp needs a measured point).
- With this, the runtime serves all ten monitoring kinds, so the plan no longer refuses a kind by name. The
  refusal mechanism stays in the plan for a kind the runtime does not serve in the future; its test lowers the
  served set, and a test checks that the set equals the kinds of the model. The refusal of D-019 is therefore
  closed in practice.
- Contract: a new point kind is additive, so the contract is 1.6.0.
- Tests: `points.rs` (the forms, the refusals, the simulation refusal), `image.rs` (the packed and unqualified
  readings), `tests/station.rs` (the two kinds are served, and their updates reach a client as M_PS_NA_1 and
  M_ME_ND_1 readings with their values), `tests/unit/test_docker_plan.py` (every model kind is served).

## D-025: The link flags of the runtime (S5)

- Date: 2026-10-11 (task S5).
- Decision: the runtime takes `--t0`, `--t1`, `--t2`, `--t3` (whole seconds), `--k`, and `--w` (1 to 32767), with
  the defaults of §9.6 when a flag is absent. The values are checked with the rules of the upstream link
  configuration (`LinkConfig::new`, the same rule the library applies to a station and to a client): a timeout
  out of its range, t2 not below t1, and a window out of its range are refused, naming the parameter. The check
  runs when the command line is read, and again when the runtime binds, so a configuration built in code is
  checked too. The runtime's configuration is boxed in `Invocation::Run` (the parameters make it large).
- The plan passes all six flags to every runtime, the defaults included, so the values a run uses are the ones
  in its plan (`docker/plan.py`). The plan no longer refuses link parameters; its refusal, of D-016, is removed.
- Contract: no change. The flags are command-line arguments; the control API does not report them.
- Tests: `config.rs` (the defaults; each flag sets its parameter; each library rule refused by its parameter;
  the station checks its own role), `tests/client_role.rs` (a t1 of 2 seconds ends a link whose start is never
  confirmed, in under 9 seconds; the default t1 of 15 seconds does not, which the test was checked against),
  `tests/unit/test_docker_plan.py` (the flags are in the arguments of a client and of a station), and
  `tests/integration/test_docker_bench.py` (a bench with non-default parameters starts, and the flags are in the
  container's command).

## D-026: The assertions and the observation source (E2)

- Date: 2026-10-11 (task E2).
- Decision: the assertion matchers compare an observation with a step: the kind of the point (from its type),
  the value (a number within the tolerance, or an exact boolean, text, or object), the common address, the cause,
  the time tag (`stamped`), and the quality flags the step names. A condition the step does not name is not
  checked. Each mismatch is a message that gives what was received and what was expected; a failed step lists
  every mismatch. `assert_point` reads the point image as it stands; `expect_point` waits for an observation
  after the step's cursor that meets all the conditions. When its deadline passes first, the verdict says what the
  last observation lacked: the handler leaves a note on its context, which the engine adds to the TIMED_OUT verdict.
- Models: the two assertion steps gain the optional conditions: `common_address`, `cause` (1 to 63), `stamped`, and
  `quality` (a set of optional flags). The published scenario schema is regenerated.
- The observation source is an interface (`Source`), tested with a fake runtime. The runtime's events do not carry
  decoded values today (only the ASDU bytes), so a source over the control API waits for E2b, which makes the
  runtime decode the observations into its events (a contract change), as the image already does.
- Not in E2: the other step actions (waits, updates, commands, restarts), which come with E3 to E5.
- Tests: `tests/unit/test_scenario_assertions.py` (each matcher with its tolerance and its message; the point image;
  the wait that never uses an observation from before its cursor; the timeout message; a scenario of both kinds) and
  `tests/unit/test_scenario_engine.py` (updated for the source's image).

## D-027: Cancellation and cleanup of a run (E3)

- Date: 2026-10-11 (task E3).
- Decision: a run is cancelled by setting an event that the caller holds (`execute(..., cancel=...)`). A step in
  progress ends CANCELLED: its handler is cancelled and awaited, so its `finally` blocks run before the run ends.
  A cancel that arrives before a step stops the run before that step (`cancelled before step N`). The steps after
  a cancelled step do not run.
- Cleanup: the scenario's cleanup steps run in order after success, failure, cancellation, and the global
  deadline. They are not subject to fail-fast, each gets a verdict named `cleanup N: <action>`, and they cannot be
  cancelled. A cleanup step always has a deadline: its own `timeout`, or `CLEANUP_SECONDS` (the default step
  timeout, 10 seconds) when it names none. Without that bound a cleanup step could hang after the global deadline.
- Status: an ERROR or UNSUPPORTED verdict, in a step or in cleanup, makes the run ERROR. Otherwise a cancelled run
  is CANCELLED, even when a cleanup step failed: that failure stays in the verdicts. Otherwise a non-passed verdict
  or the global deadline makes the run FAILED, and the run is PASSED when every verdict passed.
- Not in E3: `execution_seconds` of an instance (D2 left it to E3). It bounds the lifetime of the bench's
  containers, which the run controller owns (M2 and D3), so the engine does not enforce it here.
- "Leaves no pending command": there is no command action in the engine yet. The contract is the handler's: a
  handler holding a request must release it in a `finally` block, which a cancellation runs. A test checks this
  with a fake request. The check must be repeated when the command handlers land.
- Tests: `tests/unit/test_scenario_engine.py` (a cancelled step is stopped and its request closed before the run
  ends, and the next step does not run; a cancel between steps; a cancel before the start runs only the cleanup;
  cleanup after success, failure, and the global deadline; every cleanup step runs under fail-fast; a failed cleanup
  step fails a passed run; an unsupported cleanup step is an error; a cleanup step is bounded by its deadline after
  the global one).

## D-028: The runtime's point events and the source over the control API (E2b)

- Date: 2026-10-11 (task E2b). Contract 1.7.0.
- Decision: the client runtime records one `point` event per decoded value of each ASDU, right after the `asdu`
  event that carried it (`points_of` in `events.rs`, the decoding of the point image). Each point event carries
  its address, type identification, value (as the image reports it), cause, common address, quality, and time tag.
  The engine matches one observation per event with its own sequence number, so the values are not nested in the
  `asdu` event.
- Contract (1.7.0): `events.schema.json` gains the `Point` definition; the `ImagePoint` of `runtime-api.yaml` gains
  `common_address`, so that an assertion on the image has the same fields as one on an event. The versions of the
  three contract files and of `CONTRACT_VERSION` move together.
- Source: `runtime/source.py` (`ControlSource`) implements the engine's `Source` over the control clients of the
  client runtimes, one per scenario instance. An image point has no sequence number, so `Observation.seq` is
  now None for it.
- Known limits, to address later: `last_seq` reads every kept event (the route has no cheaper query), and the
  source does not see the events dropped from the runtime's log; a step that waits for a dropped event times out
  rather than failing. The point events double the number of events of a station with many points, so the event
  log (10000 by default) fills sooner; the reader sees the drop through `dropped`.
- The Rust contract checker (`tests/contract.rs`) reads no `anyOf`, so the `value` of a point has no type in the
  schema: the Python contract test (full JSON Schema) is the stricter check.
- Tests: `runtime/.../tests/client_role.rs` (the point event follows its ASDU with the same cause, common address,
  and value; the image holds the common address); `tests/contract.rs` (the `point` sample matches its definition);
  `tests/unit/test_runtime_source.py` (the mapping of events and image points, the cursor, an unknown instance);
  `tests/integration/test_runtime_scenario.py` (an assertion scenario against a station and a client: a passing
  scenario with an expectation of a spontaneous update; a wrong value times out and says what arrived; the image
  reports a wrong value and a wrong cause); `tests/integration/test_contract.py` (the events of a client include a
  `point`).

## D-029: Parallel branches and barriers (E4)

- Date: 2026-10-11 (task E4).
- Decision: a `parallel` step runs its branches at the same time, each in order. The step is its barrier: its
  verdict comes when every branch has ended. A branch step gets its own verdict as it ends, labelled by its
  position: `step 2.1.3` is the third step of the first branch of the second step (nested parallels extend the
  label, and cleanup steps use `cleanup k`).
- Fail-fast (the step's `fail_fast`, true by default): a branch step that does not pass halts the other branches.
  Their steps in progress end CANCELLED, and the steps not yet started have no verdict. Without fail-fast, each
  branch runs to its end. The barrier's verdict is PASSED when every branch step passed, and FAILED otherwise,
  naming the steps that did not pass (and saying when the other branches were stopped).
- Barrier deadline: `Parallel` gains an optional `timeout` (the schema is regenerated). When it passes, the
  branches still running are cancelled and awaited; each of their steps in progress gets TIMED_OUT ("the barrier's
  deadline passed during this step"), and the barrier gets TIMED_OUT ("no result within N s: k of m branches still
  running"). Without a `timeout`, the branches are bounded by their own step deadlines and by the scenario's.
- Cancellation: the run's cancel event halts every branch, and the barrier ends CANCELLED.
- Global deadline: every step still open is recorded TIMED_OUT, whether it is top-level, in a branch, or a barrier.
- Each step has its own `Context`, so a note left by one branch cannot reach the verdict of another.
- Implementation: the barrier waits with `asyncio.wait`, not `gather`. A `gather` answers a cancellation only
  after all its children have ended, which would hide the branches still running when the barrier's deadline
  passed (found by the deadline test).
- Not in E4: the restart and reconnect actions (E5); a parallel step in the cleanup is run, but no scenario uses one
  there yet.
- Tests: `tests/unit/test_scenario_parallel.py` (the barrier waits for every branch, and the branches start
  together; a failed branch step under fail-fast stops the others; without fail-fast every branch runs to its end;
  a deadline during the barrier; the global deadline during the branches; a cancellation during the branches; a
  parallel step inside a branch has its own barrier). The engine tests are unchanged and pass.

## D-030: Restart and reconnect: the instance actions and the local bench (E5)

- Date: 2026-10-11 (task E5).
- Decision: the actions that drive the instances of a bench are handled in `scenarios/actions.py`, over a `Bench`
  (the control API of each instance, the clients of each station, and how an instance is stopped and started
  again). Four actions are new to the handlers, and one is new to the model:
  - `start_transfer` (new to the model). The library does not restart the data transfer after it reconnects (its
    reconnect loop only re-establishes the connection), so a scenario must request the transfer again. The step
    waits until the client reports the transfer started.
  - `stop_instance` waits until each client that was connected records the disconnection (its `ready` shows the
    connection down), so that a later wait for the connection cannot see the connection from before the stop.
  - `start_instance` starts the instance again on the arguments it had, and returns once it answers.
  - `wait_connection` and `wait_transfer_started` wait on the state `ready` reports, which the runtime updates as
    it records the connection and the transfer. `general_interrogation` waits for the end of the interrogation (cause
    10) after its cursor, so an earlier end cannot satisfy it. `update_point` waits until the station stores the
    value.
- Model: `start_transfer` is added to the actions and the steps; `stop_instance`, `start_instance`, and
  `update_point` gain the `timeout` every other action has (a wait without a deadline could never end). The
  scenario schema is regenerated.
- `wait_connection` on a station is an ERROR: a station has no connection of its own, and a pass would be wrong.
- `runtime/local_bench.py` (`LocalBench`): one station and one client as local processes. The station's listening and
  control ports are chosen when the bench is made, and an instance that starts again gets the same arguments, so it
  answers on the same addresses.
- Not in E5: the restart of a container of the Docker bench. The station binds port 0 in a managed bench, so a restart
  changes its port and the client's target goes stale. A managed restart needs a fixed station port in the plan
  (D2), which is a decision of its own. The restart of a managed instance stays open.
- E3's test of the cleanup deadline changed: `stop_instance` now has its own timeout, so the cleanup deadline is
  tested with a parallel cleanup step, whose barrier takes it. A step with its own timeout keeps that timeout.
- Tests: `tests/unit/test_scenario_actions.py` (a wait for the connection passes when the client comes up; a station
  is an error; the transfer is requested and awaited; an interrogation ignores an earlier end; a stop waits for its
  clients to disconnect, passes at once without a connected client, and times out when a client never disconnects;
  an update waits for the stored value); `tests/integration/test_restart_scenario.py` (a station is restarted under
  a scenario, the client reconnects, starts its transfer, interrogates again, and reads the restarted station's
  value; a reconnect that does not come in time times out, and the cleanup starts the station again).

## D-031: The MCP server over stdio and the definition tools (M1)

- Date: 2026-10-11 (task M1).
- Decision: the server is `igs_iec104_mcp.server` on the official SDK (`mcp` 2.x, `MCPServer`), started with
  `python -m igs_iec104_mcp --project DIR` over stdio. The project directory must exist; otherwise the server stops
  with status 2 and says so on stderr. Stdout carries the MCP messages only: nothing else is printed to it.
- Tools (§9, the definition group): `bench_validate`, `bench_create`, `bench_get`, `bench_list`, `scenario_validate`,
  `scenario_create`, `scenario_update`, `scenario_get`, `scenario_list`. The business logic is in
  `definitions/service.py`, which the tools call and the tests call. Benches have no update tool in §9.
- Results: a validation is a result, not an error: `{valid, issues: [{path, message}]}`. A scenario is validated
  against its bench when the project has it, and a missing bench is an issue at `bench`. A definition, a page, and
  a revision are typed structures.
- Errors: a failed request is a tool error with a stable code, one of `invalid_definition`, `invalid_name`,
  `not_found`, `already_exists`, `stale_revision`, `invalid_argument`, `storage_error`. The SDK puts
  `Error executing tool <name>: ` in front of the text of a tool error, so the code is the first word after that
  prefix. A crash of the server is not a code: the SDK reports it without its text.
- Pagination: `bench_list` and `scenario_list` take `limit` (1 to 200, default 50) and a `cursor`. The cursor is the
  last name of a page, and the names are in order, so a page does not shift when a definition is added.
- The store gains two kinds of error: `InvalidName` (a name that is not valid, or leads outside the project) and
  `NameMismatch` (a definition whose own name is not its file's name). Both are `StoreError`s, so the existing checks
  still hold, and the service maps them to `invalid_name` and `invalid_definition`.
- Test setup: the project is not installed in the test environment (uv `package = false`), so the test starts the
  server with `PYTHONPATH` set to `src`.
- Not in M1: `bench_start`, `bench_stop`, `bench_destroy`, the instance and run tools, `target_validate` (X1), the run
  tools (M2), and Streamable HTTP (M4). The definition tools do blocking file I/O inside the tool call, which is short
  and under a lock; that is accepted for definitions and noted for the Docker tools (M2), which must not block.
- Tests: `tests/unit/test_mcp_service.py` (validation of a bench and of a scenario against its bench, creation and
  reading with revisions, a repeated create, a missing definition, bad names, invalid text, updates with a stale
  revision, pagination and its limits, and the server's exit with status 2); `tests/integration/test_mcp_stdio.py` (a
  real MCP client starts the server, lists the nine tools, validates a bench, stores one, reads it back, and sees the
  error codes). The integration job of CI runs the stdio test.

## D-032: The run store (R1)

- Date: 2026-10-11 (task R1).
- Decision: `storage/runs.py` keeps each run's record in `runs.sqlite3` (metadata, status, times, the counts of the
  journal, and the artifact index), its event journal in `runs/<id>/events.jsonl` (one canonical JSON line per event,
  numbered from 1 by the store), and its artifacts in `runs/<id>/artifacts/<name>`. The transitions of a run are the
  model's `TRANSITIONS`, which moved from the engine to `models/run.py` so that both share one table.
- Bounded storage (§10): the journal holds at most `max_events` events (10 000 by default). A further event is counted
  as dropped, and the run's record says how many. An artifact is written once and its SHA-256 is recorded; an artifact
  larger than `max_artifact_bytes` (16 MiB by default) is refused. A run that has ended takes no more events.
- Names and paths: a run's identity is 32 hex digits, an artifact's name is a plain lower-case file name, and every
  path is checked to stay in the project directory once its links are followed.
- Pages: runs are listed in creation order with a cursor, and events in journal order with a cursor; a page holds at
  most 200 items.
- Not in R1: the reconciliation of runs after a restart (D3), the reports (R2), the retention that deletes old runs
  (R3), and the MCP tools that read the store (M2). The store is one process's; a second controller must not write the
  same directory.
- Criterion (the plan gives the general rule for the R tasks, "its tests pass and its reports survive test bench
  cleanup"): the store's tests pass, and the store does not depend on a Docker resource, so a record survives the
  removal of its bench.
- Tests: `tests/unit/test_run_store.py` (21 tests: the lifecycle, the transitions and their refusals, the identities,
  the journal's order, its canonical lines, the fields the store owns, the bound and the dropped count, a run that has
  ended, the artifacts with their digest, their names and their size, unknown runs and artifacts, the pages, a page
  size out of range, the records after a reopen, and a store written by a newer version).

## D-033: The scenario run tools and the Docker runs (M2)

- Date: 2026-10-11 (task M2).
- Decision: five tools run and follow the scenarios: `scenario_run(name)` returns the run at once, running; `run_get`,
  `run_list`, `run_events` (a page of the journal, with a cursor), and `run_cancel`. The `scenarios/runner.py` module
  carries them: a run validates its scenario against its bench, refuses a cleanup policy it does not carry out, takes the
  snapshot (§5), creates the run record (R1), starts the bench through the D2 lifecycle with its containers owned by the
  run, runs the scenario in a task of the server's loop, and removes the bench afterwards, whatever the outcome. The run
  ends with a status and a reason, recorded in the journal and in the run store.
- Journal: each verdict is journaled as it is known (`verdict` events, through a new `observer` of the engine's
  `execute`), then `stopped` (or `error` when the bench does not stop cleanly) and `ended` with the status and the
  reason. A cancel request is journaled as `cancel_requested`.
- Engine: no bench runs without its images. The server takes `--runtime-image` and `--relay-image` by identity (a digest,
  as §6 requires); without them a run is refused with `engine_unavailable`. Before a run, the engine checks the engine
  answers and both images are present by identity.
- Cleanup policy: only `success_and_failure` (with `keep_evidence: true`) is carried out. Other policies are refused with
  `unsupported`, rather than leaving containers running without an owner.
- Execution time: the shortest `execution_seconds` of the bench's instances is enforced by the runner: when it passes,
  the run is cancelled and the journal records the limit (an `error` event). This closes the part of D2's deferral that
  the caller owns; the engine itself keeps no clock.
- Actions on a managed bench: the actions read and call each instance through its control API. The actions that
  restart an instance (`stop_instance`, `start_instance`) are refused as errors, since a restart of a managed instance
  needs a fixed station port in the plan (D2, as in D-030).
- Changes to existing parts: the engine's `execute` takes an observer; an update (`update_point`) is complete only when
  each client connected to the station holds the new value in its image, since a spontaneous update could be recorded
  before the cursor of a following `expect_point`. The example scenario therefore checks the value and its cause (3)
  with `assert_point`, and it requests the client's transfer first (`start_transfer`), which the library does not start
  by itself (D-030). The error codes gain `invalid_state`, `unsupported`, and `engine_unavailable`.
- Not in M2: the resources for configurations and reports (the reports come with R2); the bench and instance tools of §9
  (`bench_start`, `bench_stop`, `bench_destroy`, `instance_get`, `instance_logs`) because the runs manage their benches;
  reconciliation of runs left running by a restart (D3); external targets (X1 and M3); Streamable HTTP (M4).
- Milestone 5 criterion: `tests/integration/test_mcp_run.py` — a real MCP client creates the bench and the scenario,
  runs the example scenario on a Docker bench, follows the run to `passed` (six verdicts, journaled), lists the run, and
  finds the bench's containers removed. It passes locally on Podman with the runtime and relay images built from the
  repository. The CI integration job runs it on Docker; that check is confirmed when the commit is pushed.
- Tests: `tests/unit/test_scenario_runner.py` (10 tests against a fake engine: a run passes and journals its verdicts
  before its end; a bench that does not start or does not stop makes the run an error; a cancelled run ends cancelled and
  removes its bench; a cleanup policy the run does not carry out is refused; no engine refuses the run; an unknown
  scenario is not found; a cancel of an ended run is an invalid state; a bad cursor is refused; the journal is paged);
  `tests/unit/test_scenario_actions.py` (the update waits for the connected client); `tests/integration/test_mcp_stdio.py`
  (the fourteen tools are listed).

## D-034: External targets: the operation policy and the explicit probe (X1)

- Date: 2026-10-11 (task X1).
- Decision: the model of an external target (C4) already lists its allowed operations. X1 adds the one rule that
  applies them: `models/target.py` `undeclared(target, required)` names the operations a step needs that the target does
  not list. The scenario checker uses it for a command to an external target (the only operation a step can reach on one
  today), so a scenario that needs an undeclared operation is refused before any run starts, by the same check the run
  uses (`check_scenario`). A step that would need a new operation on a target extends the mapping, not the checker.
- Static check: `target_validate(bench, target)` reports the target's endpoint, common address, whether it is secured,
  and its allowed operations. It reads the stored bench and connects to nothing. The stored bench must check, and the
  target must be one the bench declares.
- Connectivity probe: explicit and opt-in. The tool runs a probe only when `probe` is set, with a timeout of 0.1 to 10
  seconds (3 by default). A probe is a TCP connection, opened and closed again, with no IEC 60870-5-104 frame sent: it
  is not an operation of the policy. It runs only for a target the stored bench declares, so a request cannot reach an
  address the bench does not name. A peer may log the connection attempt.
- Not in X1: a confirmation for a state-changing operation (M3, through MCP elicitation, and the opt-in of the target
  definition); the external connections of a bench, which the planner refuses until X2 and M3; a secured probe, which
  checks TCP only and does not attempt the TLS handshake; the endpoint of a published server (X2).
- Tests: `tests/unit/test_external_targets.py` (8 tests: the undeclared operations; a command to a target that does not
  allow it is refused, and one that allows it passes; the static report makes no connection; a probe connects once and
  sends nothing; a probe of a reserved, refusing port reports the failure; an undeclared target and an unknown bench are
  not found; a probe timeout out of range is an invalid argument), and the MCP tool list in `test_mcp_stdio.py`.

## D-035: Published station ports reached from outside Docker (X2)

- Date: 2026-10-11 (task X2).
- Decision: a station can be published: its instance declares `publish` with the port it listens on in the bench
  (imposed, since a published port cannot be picked by the runtime), the host port, the host address (`bind`, loopback by
  default), and the endpoint it advertises to external clients (`advertise`, declared, never discovered). Only a station
  can be published, and two stations cannot publish one host port.
- Engine: a published station gets its own relay container, on the control network (the only network with a route to
  the host, D-018), which listens on the station's port and forwards to the station's address. The engine publishes the
  relay's port on `bind`:`host_port` as the plan names it, never a port it picks. The relay joins the station's network,
  as the control relay does. Its logs are kept with the evidence, and the owned resources are removed with the bench.
- Spec: `ContainerSpec` gains one publication (`PortPublication`), with its ports and address checked. A container
  publishes its control port or one port, not both. The field set test records the new field: the safety of the spec
  is that it has no mount, privilege, or capability field, not that its fields never grow.
- Advertised endpoint: `RunningBench.advertised(instance)` returns `advertise:host_port`, as declared.
- Not in X2: the Docker Desktop and WSL check named by the done-when of X2. The test ran here on Linux with a Podman
  engine on WSL2; Docker Desktop is not available here. That check belongs to the platform matrix (V4), so X2 stays
  open until its maintainer decides how to record the Linux result.
- Tests: `tests/unit/test_publication.py` (the publication's defaults and refusals; only a station; the imposed port; the
  relay's arguments; a duplicate host port refused; the ports the engine is asked for); `tests/integration/test_docker_publish.py`
  (a runtime on the host connects to the advertised endpoint, starts its transfer, interrogates, reads 12.5, the bench is
  removed, and the stop is graceful). The whole integration suite passes on Podman (24 tests).

## D-036: X2 closed on the Linux result

- Date: 2026-10-11 (task X2, closed after the commit of D-035).
- Decision: the user accepts the Linux check (Podman on WSL2, `tests/integration/test_docker_publish.py`) as the
  done-when of X2 for now. The box is ticked. The Docker Desktop and WSL check is not dropped: it is recorded as an
  open item of the platform matrix (V4), to be run on a machine that has Docker Desktop before a release.
- Why: Docker Desktop is not available on the development machine, and the publication path (the relay on the control
  network, the host port, the advertised endpoint) is the same engine API on both platforms.
- Not changed: the plan text of X2 still names both platforms. The status line in IMPLEMENTATION.md §4 says what is done.

## D-037: Reconciliation of the runs and recovered benches (D3)

- Date: 2026-10-11 (task D3).
- Decision: when the server starts, each run the store holds as unended (pending or running) and that no task of the
  server runs is marked interrupted. Its journal gets an `interrupted` event with the reason, and the run ends with
  status `interrupted`. A pending run goes through `running` first, as the transitions require. Nothing is resumed: no
  command is sent again, no bench is started, and no verdict is invented for a step that was in flight.
- Benches: the Docker engine lists the benches it holds for the project, from the labels of its resources (one owner per
  run and bench). `bench_recovered` lists those whose run this server does not run, with the status of the run when the
  store knows it. `bench_recovered_remove` removes one bench by its run: the logs and records of its containers are
  kept in the run's evidence, then its owned resources are removed. A run this server runs is refused (`invalid_state`),
  and a run without a bench on the engine is `not_found`.
- Removal is explicit. Nothing is removed at startup, so a bench stays available for inspection until someone asks.
- Engine: the listing and removal need the engine, not the images, so they work on a server started with images and
  refuse with `engine_unavailable` on a server started without them.
- Tool names: `bench_recovered` and `bench_recovered_remove`. §9 names `bench_destroy` for managed benches; it was not
  reused, since the recovered bench is one this server does not manage.
- Not in D3: the restart of an interrupted run (a new run is started instead); the removal of benches at startup; the
  outcome of a command that was in flight when the controller stopped, which is unknown and is not reported as a
  verdict.
- Tests: `tests/unit/test_run_reconcile.py` (9 tests, with the fake engine of M2); `tests/integration/test_docker_reconcile.py`,
  where a controller process is killed (SIGKILL) after its first step, a new controller marks the run interrupted, lists
  the bench, removes it with its logs kept in the evidence, and finds no owned resource left.

## D-038: Confirmation of commands to external targets (M3)

- Date: 2026-10-11 (task M3).
- Decision: a `send_command` step to an external target is confirmed when the step runs, before anything is sent. The
  confirmation is taken, in order, from: (1) MCP elicitation, when the client supports it, where a person answers a
  question (only an accepted answer with `confirm` true is a yes; a decline, a cancel, or no answer is a no); (2) a
  reviewed opt-in in the target definition, when the client cannot be asked; (3) none, which refuses the command. An
  opt-in does not replace a person the client could ask: when elicitation is supported, the person decides.
- Definitions: `ExternalTarget.opt_in` (optional): the reviewer, the review date as `YYYY-MM-DD` (an unquoted YAML date
  is accepted and kept as its text), and the state-changing operations it covers. An opt-in covers only operations the
  target allows, and only state-changing ones. The bench schema is regenerated.
- Journal: one `confirmation` event per decision, with the operation, the target, the address, whether it was accepted,
  how it was decided (`elicitation`, `definition`, or `none`), and the words of the decision. A client that does not
  answer confirms nothing, and its refusal is recorded.
- Wiring: `scenario_run` gives the run the client's session. The question is sent by the run's task, after the tool has
  returned. `scenarios/confirmation.py` holds the decision, the elicitation adapter, and the handler of `send_command`,
  which refuses a command to a target that does not allow it (a check the start already makes), and a command to a
  managed client, which stays unsupported.
- Outcomes: a refused or declined command fails its step. A confirmed command is not carried out, because the planner
  does not connect external targets: it is UNSUPPORTED, and the run ends in error. The verdict says so.
- Timing: the question shares the step's own timeout (`send_command` defaults to 10 s). A scenario that waits for a
  person sets a longer timeout. The bench has started before the step runs: the confirmation guards the command, not
  the bench.
- Not in M3: external connections (the planner still refuses them, so no command reaches a real peer); the other
  state-changing operations (set-point, clock synchronisation, counter freeze and reset), which no scenario step
  expresses yet; a confirmation for the other tools; the HTTP transport (M4).
- Tests: `tests/unit/test_run_confirmation.py` (16 tests: a confirmed, declined, and unanswered command; a reviewed opt-in
  without elicitation; a refusal without either; elicitation preferred to an opt-in; a managed client unsupported; a target
  that does not allow the command refused at start; the opt-in's rules; the answer's rules) and
  `tests/integration/test_mcp_confirmation.py` (2 tests on Podman: a real MCP client answers through elicitation and the
  decision is journaled as confirmed; a client that cannot be asked gets a refusal, and the bench is removed).

## D-039: Reports of an ended run: JSON, JUnit XML, and a summary (R2)

- Date: 2026-10-12 (task R2).
- Decision: one document is built from what a run kept (its record, its journal, its snapshot, its evidence, and its
  artifacts). The JSON report is that document. The JUnit XML and the Markdown summary are rendered from it, so the
  three formats agree. A report is made the first time it is asked for, once the run has ended, and stored as an
  artifact of the run (`report.json`, `report.junit.xml`, `summary.md`). Later requests read the stored text. A run that
  has not ended has no report (`invalid_state`), since its report would change as it runs.
- Outcomes: each step's outcome is sorted into a category. A failed assertion is `assertion_failure`; an error of the
  run or the bench is `infrastructure_error`; a deadline is `timeout`; a step the run does not carry out is
  `unsupported`; a cancelled step is `cancelled`; an outcome the run could not decide is `indeterminate`. In JUnit, an
  assertion failure is a `failure`, an infrastructure error, a timeout, or an indeterminate outcome is an `error`, and
  an unsupported or cancelled step is `skipped`, so a reader can tell the three apart.
- Per step: the label and action, the outcome and its category, the detail (the observed behaviour), the duration, the
  journal sequence of its verdict (the event reference), and the definition the label names in the run's snapshot (the
  expected behaviour). `_definition` follows the labels: `step 2.1.3` is the third step of the first branch of step 2.
- Durations: the engine measures each step from its start to its verdict, a timed-out step included. The verdict carries
  `seconds`, and the journal's `verdict` event carries it too (run schema regenerated).
- Engine identities: a run records one `engine` event before its bench starts: the runtime and relay images by their
  identities, and the upstream revision. The revision is `runtime/pin.py`, and a unit test checks it against the pin in
  `runtime/igs-iec104-runtime/Cargo.toml`, so a report names the library the run used.
- Inputs: the bench and scenario SHA-256 are the snapshot's hashes, the ones the run was checked against.
- Cleanup: the bench's stop record (`stop.json` in the evidence, with the exit codes and whether the stop was graceful),
  whether the bench stopped, and any stop failure. The cleanup steps are listed with the steps.
- Manifest: the evidence files, and the artifacts of the run that are not reports, each with its SHA-256 and size. The
  reports do not list themselves, so the JSON is the same whenever it is made.
- Survival: a report needs no engine and no bench. It is kept with the run, in the run's artifacts, so it outlives the
  removal of the bench. `run_report(run_id, format)` returns the artifact's name, digest, size, and text.
- Not in R2: MCP resources for the reports (§9 asks for resources where supported; the tool is the interface now);
  packet capture; the durations of the barrier of a parallel step (each branch step has its own); a retention policy
  for reports (R3).
- Tests: `tests/unit/test_reporting.py` (10 tests: a passed run with its expectations, inputs, and engine; each format
  stored once and read back; failures sorted into their categories; a valid JUnit with one case per step; an
  unsupported step skipped and not failed; the summary as Markdown with the failures; a run not ended refused; an unknown
  format refused; a label naming its definition; the upstream revision pinned). `tests/integration/test_mcp_report.py`
  (one test on Podman: a real MCP client runs the example, asks for the three formats once the bench is removed, finds
  the images and the upstream revision in the JSON, and reads the stored report back).
- Journal layout: the `engine` event comes first, so the tests that list a journal's kinds now expect it.

## D-040: Event buffers, storage bounds, and retention of the runs (R3)

- Date: 2026-10-12 (task R3).
- Event buffers: a runtime's event log is bounded by the instance's `event_buffer` (§6, already there). The controller
  now reads the oldest sequence number the log kept (`first_seq`). A step that reads the events after its cursor, and
  finds that events after it were dropped, raises `EventGap`: the step is an error (`infrastructure_error`), not a
  failure, and not a pass by absence. The plan forbids a silent loss of assertion evidence (§7).
- Drop counts at the end of a run: each runtime's drop count is read once the scenario has ended, before its bench is
  stopped: the `dropped` of `/v1/events` for a client, and of `/v1/station/events` for a station (a new client method,
  `station_events`). A non-zero count is a `gap` event in the journal, with the instance and the count. A count that
  cannot be read (a runtime that is down) is a `gap` too, with no count, so the report never reads as complete when it is
  not. The report lists gaps among its notes. A healthy run has none, and the integration test asserts it.
- Storage bounds: the journal bound (`max_events`, 10 000 by default) and the artifact bound (16 MiB) are the store's.
  The server takes `--max-events`. A journal that drops events says so in `journal.dropped` of each report.
- Retention: a `RetentionPolicy` keeps the newest N ended runs (`--keep-runs`) and the runs that ended within D days
  (`--keep-days`). With neither option, nothing is removed: the default keeps everything. The policy selects ended runs
  only, oldest first. `run_prune` is explicit: `dry_run` is the default, and it says what would be removed. A run whose
  bench is still on the engine is kept, so a recovered bench stays inspectable (D3). The prune needs the engine, since it
  asks which benches are there; without images it is refused with `engine_unavailable`.
- Removal: a run's record, its artifact index, its journal, its artifacts (its reports included), and its evidence are
  removed together. A run that has not ended is refused. The bench of a run is not touched by a prune.
- Not automatic: nothing is pruned at startup. The plan asks for explicit cleanup (§10), and a retention that removes
  evidence should be run by a person.
- Not in R3: a total size quota for the store; a prune of benches on their own; station-event overflow detection during a
  step (assertions read the client events, so the step-level check covers them; a station gap is recorded at the end of a
  run).
- Tests: `tests/unit/test_retention.py` (11 tests: the policy's refusals and its selection by count and by age; a dry run
  removes nothing; a prune removes the run with its journal and artifacts; a run whose bench is on the engine is kept; only
  an ended run is deleted; drops and unreadable counts are gaps in the run and its report; a clean run has none; a usage
  error for a policy that keeps nothing). `tests/unit/test_runtime_source.py` (two tests: a drop after the cursor is an
  `EventGap`, and a log that kept the events after the cursor is not). `tests/integration/test_mcp_report.py` and
  `test_mcp_run.py` pass on Podman, and a healthy run reports no gap.

## D-041: TLS in the runtime, both roles (V1a)

- Date: 2026-10-12 (task V1, first part: V1a).
- Split: V1 is three parts. V1a (this decision): the runtime secures its connections, as a client and as a station, with
  the upstream TLS settings. V1b: the controller delivers the certificate material through the archive interface and
  the bench definition names it by `secret:` references. V1c: acceptance and rejection scenarios on Docker, and the
  station's side of a refused handshake. V1 stays unticked until V1c passes.
- Decision: the runtime takes `--tls-certificate`, `--tls-key`, and `--tls-trust`, all DER, together or not at all, and
  `--tls-server-name` for a client only. The files build the upstream `Identity` and `TrustAnchors`, and the settings are
  validated against the profile before the backend is built (`src/tls.rs`). A runtime whose TLS files are missing or
  unusable does not start: `BindError::Tls` is returned before any port is bound. It never falls back to a plain
  connection.
- Client: the station's certificate is checked against the server name, or against the address the client connects to.
  The certificate must carry that name (SAN). A refused handshake is `connect_failed` in the client's events, with the
  backend's error, and the connection never comes up. The client keeps its reconnection policy, as for any failure.
- Backend: the OpenSSL backend, the upstream default (feature `openssl`). The runtime image already installs `libssl-dev`
  to build it and `libssl3t64` to run it (docker/runtime.Dockerfile).
- Contract: no change. The ready body and the events are the same; a refused handshake uses the existing
  `connect_failed` kind.
- Not in V1a: the certificate chain beyond the leaf (an intermediate certificate is not sent yet); revocation lists; the
  upstream security events (Annex A), which the runtime discards for now (`Discard`), so a security reason is not in the
  journal; the station's record of a refused client (V1c); the controller's delivery and the bench definition (V1b).
- Tests: `tests/tls.rs` (3 tests) makes a root, a station and a client certificate, and a second root, with the `openssl`
  command line at test time, so openssl must be installed where these tests run (CI runners have it): a secured client
  reaches a secured station and starts its transfer; a client that trusts another root is refused, with a `connect_failed`
  that names the handshake; a runtime whose key file is missing does not start. `src/config.rs` (5 tests) checks the
  option rules, and `src/tls.rs` (2 tests) the file and settings errors. The workspace tests, clippy with warnings as
  errors, the format check, and `cargo deny` pass; the Python tests that drive the runtime binary pass with it.

## D-042: TLS material of a bench from a secrets directory, and the intermediates (V1b)

- Date: 2026-10-12 (task V1, second part: V1b).
- Secrets: the server takes `--secrets-dir`. The directory must be outside the project directory and owner-only
  (`chmod 700`); each secret is one owner-only file (`chmod 600`), named after the secret, and not a symbolic link; a
  file over 64 KiB is refused. The reader (`definitions/secrets.py`) names the secret in its error and never shows its
  content. The content is read when a run starts, before its run record is created: a missing or unreadable secret, or
  a server without the directory, is refused as `invalid_definition`, and no run is created.
- Delivery: the material reaches each runtime through the archive interface, as the control token and the points do,
  before the container starts. It is never in the environment, a bind mount, the command line (which names only the
  files in the run directory), or the evidence. The journal records one `secrets` event with each file's instance, file
  name, reference, and SHA-256; the report lists the same in `inputs.secrets`. The tests check that the content appears
  in neither.
- Definition: an instance may declare `tls` with `certificate`, `key`, and `trust` (secret references, DER), up to eight
  `intermediates` (in chain order), and, for a client, `server_name`. A station may not declare a server name. A value
  that is not a secret reference is refused by name (the existing rule of `secret:<name>`).
- Runtime: `--tls-intermediate FILE` may repeat; the chain is the certificate, then the intermediates in order. An
  intermediate without the three TLS files is refused. The runtime image is rebuilt from this repository for the tests
  (`podman build --file docker/runtime.Dockerfile runtime`), and the CI rebuilds it as before.
- Plan: the TLS flags are added to a runtime only when its instance declares TLS, with the files of the run directory.
  The plan records the number of intermediates, not their content. A bench whose TLS material is missing is refused
  before any resource is created.
- Not in V1b: the acceptance and rejection scenarios (V1c), and the station's record of a refused client (V1c); a
  secret of an external target (its TLS names are not delivered yet); rotation of the secrets while a bench runs.
- Tests: `tests/unit/test_secrets.py` (9 tests: the directory's location and mode; the reference form; a missing
  secret; a group-readable secret, refused without its content; a symbolic link; the size limit), `tests/unit/test_instance_tls.py`
  (the definition's rules; the plan's flags and chain order; the client's server name; the material names),
  `tests/unit/test_run_secrets.py` (the engine receives the content; the journal and the report hold digests and no
  content; a server without the directory and a missing secret are refused with no run created; a plain bench takes no
  secrets), and `tests/integration/test_docker_tls.py`, which passes on Podman: a secured bench starts with its material,
  the client's TLS connection comes up, it interrogates the station, and reads 12.5; the bench is removed. The runtime
  tests (`tests/tls.rs`, 5 tests, including a station whose certificate comes with its intermediate, and a station that
  sends none and is refused) and the workspace tests pass; clippy, format, and `cargo deny` pass.

## D-043: The TLS handshakes of a station are recorded, and a scenario expects a refusal (V1c)

- Date: 2026-10-12 (task V1, third part: V1c).
- Station events: the station's handshakes raise the security events of IEC 62351-3's Annex A, and the runtime records
  each in the station's log as a `security` event: its mnemonic, its severity (`notice`, `warning`, or `alarm`), the
  clause that defines it, and the backend's detail. The log exists before the station binds, so the handshakes record
  into it. Contract 1.8.0 adds the kind to `station-events.schema.json`; the contract tests cover it.
- Why: a refused handshake makes no `connected` and no `closed` event on the station (the upstream drops the
  connection silently), so the security event is the station's only record of the refusal.
- Client events: a client's handshake events are still discarded. A refused client records its reason as the text of
  its `connect_failed` event.
- Scenario step `expect_refusal` (`client`, `server`, `timeout`): it takes both cursors at its start, then passes when
  the station records a security alarm after its cursor, or the client records `connect_failed` after its cursor. The
  verdict names the event. It fails when the station records a `connected` event after its cursor: the refusal did not
  happen. Only events after the cursor count, so an earlier refusal does not pass a later step.
- Bench lifecycle: a scenario that has an `expect_refusal` step starts its bench without waiting for its clients to
  connect, since a refused client is then the state the scenario checks. Every other scenario waits as before. The
  engine's `start` takes that choice as `await_clients`.
- Not in V1c: the client's own security events; a refusal that the client makes of the station, whose handshake alert
  the station does not report as an Annex A event, so only the client's `connect_failed` shows it; the step's
  `connected` check covers every client of the station, which is correct for the one-client benches of the tests and
  should be narrowed when a station has several clients.
- Tests: the runtime (`tests/tls.rs`, 6 tests): an accepted handshake leaves a `notice` on the station, and a station
  that does not trust the client records `TLS_NO_CA_MATCH` as an alarm and accepts no connection; the contract tests
  (the new kind matches the schema). Python: `tests/unit/test_scenario_refusal.py` (the step's checks and names; a
  station alarm, a refused client, and an accepted connection; an event before the step does not pass it; the bench's
  choice of waiting), and `tests/unit/test_contract_documents.py` (six station event kinds). Docker:
  `tests/integration/test_docker_tls_scenarios.py` runs two scenarios through the run engine on Podman: the acceptance
  passes with its three verdicts, and the rejection passes because its station records the alarm. Both benches are
  removed.
