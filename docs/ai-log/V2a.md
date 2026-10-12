# V2a — A client of a redundancy group in the runtime

## Prompt

Task V2 of `IMPLEMENTATION.md` §14: redundancy scenarios. Depends on E5 and U2. V2 is split (D-044): this entry is its
first part, the runtime's client role as a redundancy group.

## Summary

- `config.rs`: `--connect` repeats, up to eight stations; `Role::Client` holds the list.
- `client_role.rs`: a `Link` is one `Client` (unchanged for one station) or the upstream `RedundantClient`. A `Switched`
  event starts a new connection generation; a standby station's events are recorded with their `endpoint` and leave the
  correlator alone. The status follows the started station.
- `events.rs`: the `switched` kind, and the optional `endpoint` of a recorded event.
- `api.rs`: `endpoints` and `started_endpoint` in the client's ready body.
- `runtime.rs`: one TLS wrapper per station, and the link built from the configurations.
- Contracts: 1.9.0 (events, ready body, runtime API).
- D-044 records the decisions, and what is not in V2a.

Dependencies: none new (the upstream `redundancy` module is in the pinned library).

## Verification

- `cargo test --workspace --all-features` in `runtime/` passes, including `tests/redundancy.rs` (1 test: the switchover
  between two stations, with its event, its endpoint, the transfer, and the image). `tests/contract.rs` covers the
  `switched` kind and an event naming its station. The config tests cover repeated `--connect` and the limit of eight.
- clippy with warnings as errors and the format check pass.
- The Python unit suite and the MCP stdio test pass (426 tests), with the event kind count of the contract updated.
- The Docker tests of a client of one station pass with the rebuilt image (`test_mcp_run.py`, `test_docker_tls.py`,
  `test_docker_tls_scenarios.py`).

## Decisions

- D-044: the split of V2, the link, the events, the ready body, the transfer of a group, and what is not in V2a.

## Open

- A client's manual switchover (the library's `switch_to`) is not exposed through the control API.
- The group's transfer start is a no-op: a group starts its own transfer, so the control API has nothing to ask.

## Points for the next tasks

- V2b: the bench model and the planner: an instance of role client with several connections to stations of one network,
  and the planner's refusal "a client has one server" replaced by a group rule.
- V2c: stopping a managed station (stop only), and scenarios that stop the started station and expect the switchover.

## Size

About 370 lines added and 91 removed in the runtime's existing files (the client role, the events, the runtime, the
API, the configuration, the tests, and the contracts), and about 110 lines in one new test file.
