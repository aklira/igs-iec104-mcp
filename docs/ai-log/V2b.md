# V2b — A client of a redundancy group in the bench and the planner

## Prompt

Task V2 of `IMPLEMENTATION.md` §14, second part (D-044): the bench model and the planner accept one client with several
stations. V2 is split, and this entry is V2b.

## Summary

- `docker/plan.py`: a client's stations are kept in the order the bench names them (`server_of` maps a client to a tuple);
  the planner refuses a client with more than eight stations, and no longer refuses a second connection. `client_arguments`
  gives one `--connect` for each station.
- `docker/bench.py`: a client is launched with the addresses of all its stations.
- D-045 records the decisions and the limits.

Dependencies: none new.

## Verification

- `tests/unit/test_docker_plan.py` (the group is planned, its arguments carry both addresses, a group of nine stations is
  refused) and `tests/unit/test_instance_tls.py` pass. The unit suite and the MCP stdio test pass (427 tests).
- `tests/integration/test_docker_group.py` passes on Podman: a bench with a client of two stations starts, the client sees
  two stations, the group starts one of them without a request, and the bench is removed.
- The single-station Docker tests (`test_mcp_run.py`, `test_docker_tls.py`) pass with the same image.
- Gates: `ruff format --check`, `ruff check`, `mypy` (strict, 93 source files).

## Decisions

- D-045: the planner's group, the order of the stations, the limit of eight, and the unchanged external-target rule.

## Open

- A station of a group is not stopped by a scenario yet: that is V2c, which needs a managed station's stop.

## Points for the next tasks

- V2c: a managed station's stop (stop only, its evidence kept), the scenario step that stops the started station, and the
  checks of the switchover on Docker.

## Size

About 80 lines of planner and bench changes, and about 130 lines of tests.
