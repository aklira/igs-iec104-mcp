# Decision: the host port of a published station

Status: accepted (option A, approved by the maintainer on 2026-10-12); implemented in D-054. Related: X2 (D-036), X3 (D-052, D-053), the plan's rule on fixed and pre-picked
ports (IMPLEMENTATION.md §15).

## Context

A station is published to the host with `publish.host_port` (the definition must name it, `1` to `65535`;
`models/instance.py`). The planner refuses two stations with the same host port (`docker/plan.py`), and the advertised
endpoint is built from the declared value (`docker/bench.py`: `advertise:host_port`).

The X2 test (`tests/integration/test_docker_publish.py`) gets its port from `free_port()`, which picks a free port just
before the bench starts. Another process may take it first, and the start then fails. The plan says integration tests
must not depend on fixed host ports or on picking a free port before a process binds it (§15). X2 was accepted with
this race (D-036); X3 (the independent client, D-053) avoided publication for the same reason. An external client
outside Docker still needs a published station, so the race has to be settled before that client can be tested.

## Options

**A. `host_port: 0` means "the engine chooses".** The definition accepts `0`. The engine picks a free port when the
station starts, and the bench reads the port it bound from the engine, as it already reads the control port. The
advertised endpoint is `advertise:<bound port>`, and the run journal records it when the station starts. The planner
ignores `0` in its duplicate check, since each `0` gets its own port.

- For: no port is picked before it is bound; no race; the same pattern as the runtime's control port (D2).
- Against: the endpoint is no longer fully fixed by the definition, so an external client learns it from the run; the
  schema of a bench changes (`host_port` may be `0`); `0` has two meanings for a reader (to be documented).

**B. Keep a fixed host port and document the race.** The status quo of X2.

- For: no code change.
- Against: the race stays; it conflicts with the plan's rule; a second run on the same machine can fail for no reason in
  the definition.

**C. Pick a port and retry on a bind failure.** The test (not the product) retries with a new port when the engine
refuses the start.

- For: small change, test only.
- Against: still picks ports before the bind, which the plan forbids; the retry depends on the engine's message.

**D. An operator-reserved host port range.** The server is given a range of host ports, and the planner allocates from it.

- For: predictable ports for firewalls and external clients.
- Against: needs operator configuration; the range can run out; a new option on the server.

## Recommendation

**Option A.** It removes the race at its cause, follows the pattern the runtime already uses for its control port, and
keeps the change to the model, the planner, the Docker adapter, and the bench. The endpoint reported at start is the one
an external client must use; a fixed port in the definition is no longer a promise the product makes.

## Consequences of A

- `models/instance.py`: `publish.host_port` allows `0` (`ge=0`); the description says what `0` means. Regenerate
  `contracts/definitions/bench.schema.json`.
- `docker/plan.py`: the duplicate check skips `0`.
- `docker/adapter.py`: a `0` publishes with no host port named, so the engine chooses one.
- `docker/bench.py`: after the station starts, read the bound host port from the engine record of the publication
  container, and build `advertised` from it. The run journal records a `published` event with the endpoint (a runner
  change in `scenarios/runner.py`).
- `tests/integration/test_docker_publish.py`: uses `0`; the `free_port()` call goes away.
- `docs/docker.md`, `docs/definitions.md`: describe `0`, and that the endpoint is the one reported at start.
- The plan: X2's note on the race is closed; X3's external-client item depends on this decision.

## Questions for the maintainer

1. Accept `0` as "the engine chooses" in the definition (option A), or prefer a separate flag?
2. Where is the reported endpoint shown: the run journal only (proposed), or also in a tool result (`run_get`)?
3. Keep the fixed-port X2 test until A lands, or switch it in the same change?

## Resolution

The maintainer chose option A. The three questions were answered as recommended: `0` means "the engine chooses" in the
definition; the endpoint reported at start is recorded in the run journal as a `published` event; the X2 test moves to
`0` in the same change. See D-054.
