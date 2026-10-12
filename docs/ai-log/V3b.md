# V3b — The fault proxy in the benches, and the fault steps

## Prompt

Task V3 of `IMPLEMENTATION.md` §14, second part (D-050), after V3a (D-049). The component is approved; this part wires
it into the benches and adds the steps.

## Summary

- Model: a connection declares `fault_proxy`; the steps `inject_fault` and `clear_fault` are added to the scenario
  model, with their checks.
- Planner: a faulted connection gets a proxy on its network, `proxy-<n>`; the proxy arguments and entrypoint.
- Docker: the proxy container runs the relay image's program with its own control relay; the clients are given the
  proxy's address; the proxies are stopped and their logs kept; `put_files` takes the owner of the copied token.
- Relay image: `faultproxy.py` is copied in, and `/run/igs` is created for the relay's user.
- Runner and actions: the handle gives the fault client of a connection; the two steps are implemented.
- D-050 records the decisions.

Dependencies: none new.

## Verification

- The unit suite passes (475 tests), with ruff, ruff format, and mypy strict.
- `tests/integration/test_docker_faults.py` passes on Podman: the client connects through the proxy, starts its transfer,
  interrogates its station, and the proxy reports mode `none` with the connection it carries; the proxy's logs are kept
  and the bench is removed.
- The full integration suite passes with the new relay image: 36 tests, none skipped, on Podman (release binary, runtime
  `v2a`, relay `v3b`).

## Problems found

- A single-station client does not start its transfer by itself: the test requests it (`start_transfer`), as the
  station's client does. Only a group starts its transfer on its own.
- The `ContainerSpec` guard test listed the fields of a container. The new `entrypoint` is an image override, not a mount,
  privilege, or capability, so the list changed with a comment saying why.
- The mypy strict run found the fakes of the bench and handle without the new accessor; each fake has it now.

## Decisions

- D-050: the proxy wiring, the token owner, the steps, and what is not tested yet.

## Open

- The Docker scenarios of an interruption, a latency, and the reconnection after a clear: V3c.
- The inject and clear steps are not run on Docker yet; they are tested with fakes and by the proxy's own tests.

## Points for the next tasks

- V3c: scenarios on Docker, an interruption that the client sees and recovers from after the clear, and a latency that
  delays the interrogation within a bounded time.

## Size

About 330 lines of source (planner, bench, steps, model, and the proxy's entrypoint), and about 350 lines of tests.
