# V1c — TLS acceptance and rejection scenarios

## Prompt

Task V1 of `IMPLEMENTATION.md` §14, third part (D-041): acceptance and rejection scenarios on Docker, with the station's
side of a refused connection. V1 is done when its parts are.

## Summary

- Runtime: the station's handshakes record their Annex A security events in its log (`security` kind, contract 1.8.0);
  the log is created before the station binds, and the TLS settings take the receiver. `src/tls.rs`,
  `src/runtime.rs`, `src/station_log.rs`, and the contracts.
- Scenarios: `expect_refusal` (`client`, `server`, `timeout`) passes on a station alarm or a client's `connect_failed`,
  after its cursors, and fails if the station accepts a connection. `models/scenario.py` (the model and the checker),
  `scenarios/actions.py` (the handler).
- Lifecycle: a scenario with a refusal step starts its bench without waiting for its clients (`await_clients`), through
  `scenarios/runner.py`, `docker/bench.py`, and the engine.
- D-043 records the decisions and what is not in V1c.

Dependencies: none new.

## Verification

- `cargo test --workspace --all-features` in `runtime/` passes, including `tests/tls.rs` (6 tests) and the contract tests;
  clippy with warnings as errors passes.
- The unit suite and the MCP stdio test pass (426 tests), with the new step's checks and handler, and the six station event
  kinds.
- `tests/integration/test_docker_tls_scenarios.py` passes on Podman with the runtime image rebuilt from this repository: the
  acceptance scenario passes with three verdicts, and the rejection scenario passes with its station's alarm.
- The Docker regression tests pass with the same image: `test_mcp_run.py`, `test_docker_tls.py`, `test_docker_publish.py`,
  `test_mcp_report.py`, `test_docker_reconcile.py`, and `test_mcp_confirmation.py`.
- Gates: `ruff format --check`, `ruff check`, `mypy` (strict, 92 source files), and the schema check.

## Decisions

- D-043: the station's security events, the step, the bench's choice of waiting, and what is not in V1c.

## Open

- The client's own security events are discarded; a refused client's reason is its `connect_failed` text.
- A station with several clients: the step's `connected` check covers all of them, which the one-client tests do not
  exercise.

## Points for the next tasks

- V2 (redundancy) and V3 (controlled faults, a human gate) follow. V3 needs a maintainer's approval of its component.
- A scenario catalogue (§14 phase 8) can now include the TLS acceptance and refusal as examples.

## Size

About 417 lines added and 51 removed in the tracked files (the runtime, the Python model, runner, and bench, the contracts, and the tests), and about 450 lines in three new files, most of them tests.
