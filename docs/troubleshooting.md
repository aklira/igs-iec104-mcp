# Troubleshooting

Each entry gives what you see, what usually causes it, and what to do. Error codes are those of the [tools](tools.md#error-codes).

## The server does not start

**`igs-iec104-mcp: the project directory '...' does not exist`** (exit status 2). The project directory must exist
before the server starts. Create it with `mkdir -p`.

**`the secrets directory ... is inside the project directory`**. Move the secrets out of the project. The project is
versioned; the secrets must not be in it.

**`the secrets directory ... is readable by others`**. Make it owner-only: `chmod 700 <dir>`.

**`ModuleNotFoundError: No module named 'igs_iec104_mcp'`**. The package is not installed in the environment, and
the server is run from the source tree. Set `PYTHONPATH` to the repository's `src` directory, and start the
interpreter of the `.venv` created by `uv sync --locked`.

## A run cannot start

**`engine_unavailable`: the server has no runtime image**. Start the server with `--runtime-image` and
`--relay-image`, both set to image identities (see [setup](setup.md#build-the-runtime-and-the-images)).

**`engine_unavailable` with a Docker message** (for example, the socket cannot be reached, or the image is not on
the engine). Check the engine: `docker version` (or `podman version`) must answer from the same user and environment
as the server. For Podman, the socket service must be running and `DOCKER_HOST` must point to its socket. The
images must be built on that engine, not on another one.

**`the bench did not start: image '...' is not a digest`**. The server takes the image identity (`sha256:...`) or a
repository digest. A tag such as `igs-iec104-runtime:local` is refused, because a tag can change under a run. Read the
identity with `docker image inspect --format '{{.Id}}' <tag>` and restart the server with it.

**`the bench did not start: ...`** with another message. The run records the engine's message in its journal, as an
`error` event. Read it with `run_events`: it names the step of the start (the network, the container, or the
readiness of a runtime).

**`scenario_run` refused with `unsupported`**. The bench declares a cleanup policy the runner does not carry out (only
`remove: success_and_failure` with `keep_evidence: true` is supported). Remove the `cleanup` block, or set its
default values.

**A client does not connect to its station.** Check the bench's networks: a client and its station must be on the
same network, or the planner refuses the bench before it starts. A connection to an external target is also refused
by the planner, with the message `external targets are not managed yet (task X1)`. That message is out of date: the
external target policy is done, and the connection itself is still not supported. See
[definitions](definitions.md#external-targets).

## A run fails or times out

**A step ends `timed_out` with `no result within N s`.** The expected event did not arrive before the step's deadline.
Look at the events after the step in `run_events`: a connection that never came up, a transfer that did not start, or a
value that did not match are all visible there. Raise the step's `timeout` only after you know what it waited for.

**A step ends `indeterminate`.** A request was sent, and the connection ended before its answer. The runtime does not
send it again, and neither does the server. Decide from the station's state whether to repeat the action in a new run.

**A step ends `unsupported`.** The step asks for something the runtime or the bench does not support, such as a
bitstring command. The [platform matrix](platform-matrix.md#feature-support) lists what is supported.

**`start_instance` ends `error`: `cannot be started on a managed bench yet`.** This is by design. A managed station
is stopped for good; a restart needs a fixed station port, which the plan does not impose yet.

**A run is `interrupted`.** The server stopped while the run was in progress, and the run is not resumed. Its bench may
still be on the engine: `bench_recovered` lists it, and `bench_recovered_remove` removes it, keeping its evidence.

**A report shows a `gap`.** A runtime's event log dropped events, or its drop count could not be read. The
evidence of that instance is incomplete for the run. Raise the instance's `event_buffer`, or the server's
`--max-events` for the journal, and run again.

**A station records a `TLS_NO_CA_MATCH` security alarm.** The station refused a client's certificate, because the
client's chain does not end in the station's trust root. The alarm is a `security` event in the run's journal, and an
`expect_refusal` step passes on it. Check the station's `trust` reference and the client's `certificate`: its chain
must end in that root. A missing intermediate gives the same alarm; list the intermediates in chain order.

**`expect_refusal` fails with `accepted a connection`.** The station accepted the client, so the refusal did not
happen. Check the TLS material of both sides, and whether the station is the one the step names.

## Definitions

**`invalid_definition` with a path such as `steps[2].timeout`.** Fix the field at that path. Every problem of the text
is listed, so one pass is usually enough.

**`stale_revision` on `scenario_update`.** The scenario changed since you read it. Read it again with `scenario_get`,
merge your change, and send the new `revision`.

**`invalid_name`**. A name is lowercase: a letter, then up to 62 lowercase letters, digits, or hyphens.

## Reports and retention

**`run_report` returns `invalid_state`.** The run has not ended. Wait for its status to become terminal, then ask
again.

**`run_prune` removes nothing.** The default is a dry run: pass `dry_run: false` to remove. Also check that the
policy keeps fewer runs than exist (`--keep-runs`, `--keep-days`), and that the runs are ended and their benches are no
longer on the engine.

**`run_prune` is refused with `invalid_argument`.** The server was started without a retention policy. Start it with
`--keep-runs` or `--keep-days`.

## Platform

**The Docker tests are skipped.** A variable of the integration tests is not set (`IGS_RUNTIME_IMAGE`,
`IGS_RELAY_IMAGE`, or `IGS_RUNTIME_BINARY`). In CI the same missing input fails the run.

**The Podman socket disappears.** The `podman system service` process stopped, or its socket directory was removed.
Recreate the directory and start the service again. A run that was in progress is marked `interrupted` the next time
the server starts, and its bench is listed by `bench_recovered`.
