# E2b — Runtime-backed observation source

## Prompt

Task E2b of `IMPLEMENTATION.md` §14: the runtime's events carry the decoded observations of each ASDU (a contract
change), and a source over the control client's events and point images, which the scenario engine uses against a
real station. Done when an integration test runs an assertion scenario against a station and a client of the runtime.

## Summary

- Runtime (contract 1.7.0): the client records one `point` event per decoded value of each ASDU, right after the
  `asdu` event (`events.rs`, `points_of`, and `client_role.rs`). The point image carries the common address
  (`image.rs`, `api.rs`). `time_text` moved from `api.rs` to `image.rs`, where both users can reach it.
- Contract: `events.schema.json` gains `Point`; `runtime-api.yaml` gains `common_address` on `ImagePoint`; the
  versions of the contract files and of `CONTRACT_VERSION` are 1.7.0.
- Python: `runtime/api.py` gains `image_points`; `runtime/source.py` gives `ControlSource`, the engine's `Source` over
  the control clients of the client runtimes; `Observation.seq` is None for an image point.
- D-028 records the decisions and the known limits.

Dependencies: none new.

## Verification

- Rust: `cargo fmt --check`, `cargo clippy --workspace --all-targets --locked -- -D warnings`, and
  `cargo test --workspace --locked` pass (the new point assertions in `tests/client_role.rs`, the `point` sample in
  `tests/contract.rs`).
- Python: the unit suite (257 pass, including `tests/unit/test_runtime_source.py`); `ruff format --check`,
  `ruff check`, and `mypy` (strict, 57 source files).
- Integration, with the runtime binary built by `cargo test`:
  `tests/integration/test_runtime_scenario.py` (3 tests, run 3 times, all pass): a passing scenario with an
  expectation of a spontaneous update, a wrong value that times out and says what arrived, and an assertion on the
  image that reports a wrong value and a wrong cause. `tests/integration/test_contract.py` (the events of a client
  include a `point`) and `tests/integration/test_milestone1.py` pass.

## Decisions

- D-028: one `point` event per value, after its ASDU; the common address in the image; the source over the control
  API; the limits (the cost of `last_seq`, the dropped events, the Rust checker's lack of `anyOf`).

## Points for the next tasks

- A cheaper query for the last sequence number, or a per-instance cursor, would remove the full read of `last_seq`.
- The source does not detect events dropped from the runtime's log; a run that depends on a dropped event should
  fail rather than time out (for the run controller, M2).
- The point events double the event count of a station with many points: the event log's capacity matters for long
  runs (R3, the limits and retention).
- E4 (parallel branches) and E5 (restart and reconnect) follow.

## Size

About 150 lines of runtime and contract change, about 150 lines of Python source and tests, and about 200 lines of
tests and integration. The total is above the roughly 400 lines of §2: the contract change, its Rust consumers, and
the first runtime-backed test belong together, so the change is kept as one task.
