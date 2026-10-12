# V3c — The fault scenarios on Docker

## Prompt

Task V3 of `IMPLEMENTATION.md` §14, third part (D-051), after V3a (D-049) and V3b (D-050). It adds the Docker scenarios
of an interruption with its reconnection, and of a latency.

## Summary

- `tests/integration/test_docker_fault_scenarios.py`: two scenarios on a bench whose connection goes through a proxy,
  run by the run engine on Docker: an interrupt and reconnect, and a latency interrogation.
- D-051 records the scenarios and their limits.

No source change. Dependencies: none.

## Verification

- The two scenarios pass on Podman (relay image `localhost/igs-iec104-relay:v3b`, runtime `v2a`): the interrupt scenario
  passes its eight steps, with the interrupt and clear verdicts naming the fault; the latency scenario passes its six
  steps, and its interrogation takes at least 0.2 s.
- The unit suite passes (475 tests), with ruff, ruff format, and mypy strict.
- The full integration suite passes: 38 tests, none skipped, on Podman (release binary, runtime `v2a`, relay `v3b`).

## Problems found

- None in the code. The first run of each scenario passed.

## Decisions

- D-051: the two Docker scenarios and what they show.

## Open

- A stop of the proxy's own container during a run is not tested; the stop of a station is covered by E5 and the
  switchover tests.
- Latency is tested as a lower bound on the interrogation's time; no upper bound is asserted, since real scheduling is not
  deterministic (§8).

## Points for the next tasks

- V3 is done. The remaining plan items are V4 (the platform matrix, after M4, X3 and V3, which are now all done except X3),
  X3 (interoperability, a human gate), and V5 (release, a human gate).

## Size

About 150 lines of tests, and no source change.
