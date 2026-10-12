# X3b — The independent client against a managed station (the other direction)

## Prompt

Task X3 of `IMPLEMENTATION.md` §14, second part (D-053). The maintainer chose option (b) of D-052 on 2026-10-12.

## Summary

- `docker/independent/Client.java`: a client of this project, which drives the library's public API as a black box.
  It connects to a station, starts the data transfer, interrogates, prints the points, and exits 0 when the
  interrogation ends.
- `docker/independent/Dockerfile`: builds both programs; the station stays the default entrypoint.
- `tests/integration/test_interop_j60870_client.py`: a managed station with one point; the peer's client on the bench
  network; the test checks the peer's output and the station's record of the connection.
- D-053 records the decision, the correction of how option (b) had been described, and the open decision on the host
  port.

Dependencies: none added. The planner is unchanged.

## Verification

- `tests/integration/test_interop_j60870_client.py` passes on Podman: the peer's client exits 0, prints the point
  (IOA 100, value 12.5) and the end of the interrogation, and the station records the connection. The bench is removed,
  and no container is left behind.
- Both interoperability tests pass together (X3a and X3b).
- The unit suite passes (475 tests), with ruff, ruff format, and mypy strict.

## Clean-room record

- The peer's source was not read. The API used by `Client.java` was read from the release's javadoc, as for X3a: the
  constructor and setters of the client builder, the transfer start, the interrogation method, and the information
  classes.

## Problems found

- The information classes are in the `ie` package. The first build of `Client.java` used the wrong package for
  `InformationObject`; the compile error named it, and the import was fixed.
- The peer's client needs an entrypoint override, because the image's default entrypoint is the station. The test sets it.

## Decisions

- D-053: the peer's client on the bench network, the planner unchanged, and the open decision on the host port.

## Open

- The external client outside Docker, which needs a host port the engine chooses (D-053).
- The maintainer's review of the results and the feature matrix, which the plan requires before X3 is done.

## Points for the next tasks

- If the planner accepts `host_port: 0` with the engine's choice reported, the X2 test and an external run of the peer can
  use it with no race.

## Size

About 110 lines of client program, and about 120 lines of test.
