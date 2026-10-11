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
