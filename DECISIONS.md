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
  extra capability. Every container is created read-only, privileged false, with all capabilities dropped and
  `no-new-privileges`. Its memory, CPUs, and processes are bounded by `Limits`.
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
  interface, into an in-memory directory (`/run/igs`, `mode=0755`). They are owned by the runtime's user
  (UID 10001, fixed in `docker/runtime.Dockerfile`) and read only by it (mode 0400). They are never in the
  environment, a bind mount, or the evidence. The controller writes the token to a temporary directory
  outside the project, which is removed when the start ends.
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
