# V3a — The fault proxy: the component and its control API

## Prompt

Task V3 of `IMPLEMENTATION.md` §14, first part (D-049). V3 is a human gate: the maintainer approved the component, a
user-space proxy without privileges. V3 is split into V3a (the component), V3b (the bench wiring and the scenario steps),
and V3c (the Docker scenarios).

## Summary

- `docker/faultproxy.py`: a TCP proxy (standard library only) with three modes (none, latency, interrupt) and a control
  API protected by the bench's token.
- `tests/unit/test_fault_proxy.py`: seven tests on real loopback connections, with ports chosen by the system.
- D-049 records the decision and the split.

No dependency added. No bench, image, or scenario changed yet.

## Verification

- `tests/unit/test_fault_proxy.py` passes (seven tests, three runs in a row).
- The unit suite passes (461 tests), with ruff, ruff format, and mypy strict.
- The program was run as a process on real ports: `curl` gets `401` without the token, reads the state with it, sets a
  latency, and gets `400` for an unknown mode. The token does not appear in the log.

## Problems found

- A refused connection, closed while the client's byte was still unread, reaches the client as a reset rather than an
  end of stream. The test treats both as a close.
- The proxy's `Faults.set` counted each connection twice for an interrupt (both of its sockets). The count is now one per
  connection.
- A connection that was being opened when an interrupt arrived was forwarded. The proxy now checks the mode again after
  the upstream connection opens and refuses it.

## Decisions

- D-049: the fault proxy is a standalone unprivileged program; V3 is split into V3a, V3b, and V3c.

## Open

- The proxy is not in an image and is not started by any bench; V3b does that.
- The scenario steps for the faults do not exist yet (V3b).

## Points for the next tasks

- V3b: the connection of a bench that goes through a proxy; the planner's address for the client; the proxy's start after
  the station is ready; the proxy's control client in the runner; the `inject_fault` and `clear_fault` steps.

## Size

About 230 lines of proxy, and about 150 lines of tests.
