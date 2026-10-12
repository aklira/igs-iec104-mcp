# V2c — A managed station's stop and the switchover scenarios on Docker

## Prompt

Task V2 of `IMPLEMENTATION.md` §14, third part (D-044): a managed station's stop without a restart (stop only, with its
logs kept as evidence; restart still refused), and scenarios that check the switchover on Docker. V2 is split, and this
entry is V2c.

## Summary

- `docker/bench.py`: `RunningBench.stop_instance` stops one container with SIGTERM and keeps its logs and record as
  `<instance>-stopped` in the evidence.
- `scenarios/runner.py`: the managed bench stops an instance for good (a second stop is an error). The drop count of an
  instance is read before its stop, and a stopped instance is not read after it. The run's handle gets `stop_instance`.
- `scenarios/actions.py`: `stop_instance` waits for each connected client to record the loss: a connection down for a
  client of one station, the endpoint's `disconnected` event for a group client. When the started station is the one
  stopped, the step ends at the switchover, or when no station is connected left. A new `switchover` step stops the
  started station of a group and waits for the switch to another station, with the transfer started there.
- `models/scenario.py`: the `switchover` step, refused for a client of one station and for a server. The scenario schema
  is regenerated.
- `runtime/local_bench.py`: `stations_of` for the local bench, which the actions use.
- D-046 records the decisions and the limits.

Dependencies: none new. The runtime is not changed: the runtime image of V2a is used.

## Verification

- Unit tests: 436 pass, covering the stop waits, the switchover, the endpoint index, the drop counts before a stop, the
  double stop, and the model checks.
- `tests/integration/test_docker_switchover.py` passes on Podman (3 tests). The switchover moves the transfer to the other
  station, and the client reads the value there. A stop of a group station keeps the group serving, whichever station is
  started. A stop keeps the logs of the basic pair's station, and a restart is refused.
- The local-bench tests (`test_restart_scenario.py`, `test_runtime_scenario.py`, `test_milestone1.py`) pass with the
  release binary, which was built for them. The full integration suite passes: 35 tests, none skipped, with the binary
  and both images.
- Gates: `ruff format --check`, `ruff check`, `mypy` (strict).

## Problems found

- The group bench of V2b gave the second station the common address 2. The client's requests (common address 1) get a
  negative confirmation there, so a general interrogation timed out. The Docker test bench of V2c gives that station the
  client's address: two stations of a redundancy pair share their common address (§10).
- The evidence of a stop is one file per log and one per record. The test counts stopped stations by their log files,
  not by all files.

## Decisions

- D-046: the stop of a managed station is final, its logs are kept, and its wait ends at the loss (or the switchover of a
  started station); the `switchover` step; the drop count is read before a stop.

## Open

- A group station must be connected before a scenario stops it: the step does not read the history.
- A manual switchover through the control API (the library's `switch_to`) is not exposed.
- The restart of a managed station needs a fixed port in the plan (D2).

## Points for the next tasks

- V3 (controlled faults, a human gate) and V5 (release, a human gate) need a maintainer. V4 (release documentation and the
  platform matrix) includes the Docker Desktop and WSL check that X2 deferred.

## Size

About 200 lines of scenario and bench changes, and about 300 lines of tests.
