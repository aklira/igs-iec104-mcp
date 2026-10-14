# Issue #4 — Two clients on one station

## Prompt

The maintainer asked for an examination of issue #4 (a general interrogation that times out for one of two clients on
one station, and a rootless Podman network that is not removed). After the examination, the maintainer asked for the
documentation and the validation corrections.

## Summary

- `src/igs_iec104_mcp/models/scenario.py`: `check_transfers`, called by `check_scenario`. It follows the order of the
  `start_transfer` steps and refuses a `general_interrogation` or an `expect_point` for a client that another client
  superseded on the same station (D-065). A parallel step, a client of a redundancy group, and a station that a group
  connects to are not judged.
- `tests/unit/test_scenario_transfers.py`: ten tests for the order, the restart, a stopped client, two stations, a group,
  a parallel step, and the assertion that stays allowed.
- `docs/docker.md` (one station, several clients), `docs/definitions.md` (the same section, with the message of the
  check), `docs/troubleshooting.md` (the timeout of a superseded client), and `docs/platform-matrix.md` (the feature row
  and the limitation).
- `DECISIONS.md`: D-065.

## Reproduction

On the clean installation (a clone of `main` at 48cbf5d, its own `HOME`, Podman 5.7.0):

- one client, started and interrogated: passes;
- two clients started, the first one interrogated: `timed_out` at 40 s (the same with the order reversed);
- two clients connected, only one started and interrogated: passes;
- two clients started, only the last one started interrogated: passes.

The issue's first run, where the second client completes its interrogation and the first times out, has the same shape
as the second case: the second client starts last.

The network cleanup of the issue was not reproduced: the five runs of the clean installation were removed cleanly.

## Real checks

- `ruff format --check`, `ruff check`, `mypy`: pass.
- `pytest tests/unit`: 555 passed, with the ten new tests.
- The example scenario is valid against its bench; no scenario of the repository uses two clients on one station, so
  no existing scenario changes its result.

## Open points

- Not done: the failure message of the step still reads `no result within N s`. Naming the connection that closed the
  client (the `Superseded` event of the upstream library) would tell the person why. It is a follow-up.
- Not done: the rootless Podman cleanup error of the issue. It needs the issue author's Podman version, its network backend,
  how its service runs, and the owner of the rootless network process.
- The issue's expected behaviour (two clients observing the same station's updates at once) is not possible with one
  station, by the rule above. The issue's scenario needs one station for each client.
- The reproduction scripts are in the session's scratchpad, not in the repository.
- No plan task ID: the commit message will need `Task:` with the maintainer's choice.
