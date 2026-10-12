# X3a — An independent peer as a black box: our client against the j60870 station

## Prompt

Task X3 of `IMPLEMENTATION.md` §14, a human gate: "a maintainer reviews the results and the feature matrix". The
maintainer approved the candidate (the OpenMUC j60870 library, GNU GPL v3, as a black box in its own container) on
2026-10-12. This part (D-052) covers the first direction only.

## Summary

- `docker/independent/Station.java`: a station of this project that drives the library's public API. It listens,
  confirms an interrogation, and answers with one point.
- `docker/independent/Dockerfile`: the peer image. The library is fetched from Maven Central at 1.8.0, checked by its
  SHA-256, and run as a non-root user; both base images are pinned by digest.
- `tests/integration/test_interop_j60870.py`: our runtime's client interrogates the peer's station, and the test checks
  the point it receives.
- `.github/workflows/ci.yml`: the integration job builds the peer image and names it in `IGS_INDEPENDENT_IMAGE`.
- D-052 records the decision, the source discipline, the first direction's result, and the open decision for X3b.

Dependencies: none added to Python or Cargo. The library is an external program in a container.

## Clean-room record

- The peer's source code was not opened, downloaded, or read.
- The public API used by `Station.java` was read from the javadoc of release 1.8.0 (documentation of the public classes
  and methods). The javadoc jar's SHA-256 was recorded in the working notes, not in the repository.
- The binary jar was fetched from Maven Central; its SHA-256 matches the checksum Maven Central publishes.

## Verification

- `tests/integration/test_interop_j60870.py` passes on Podman: the connection is up, the transfer starts, the
  interrogation is accepted, and the point is type 13, cause 20, common address 1, value 12.5. The peer logs the data
  transfer start. The container is removed afterwards, and no container is left behind.
- The unit suite passes (475 tests), with ruff, ruff format, and mypy strict.
- The CI job change is checked as YAML only; the job itself runs on the next push.

## Problems found

- The library's jar was downloaded with mode 0600 (the image build copies it as root), so the peer's user could not read
  it, and the peer failed with `NoClassDefFoundError`. The build now makes the jar readable (`chmod 0644`) after the
  checksum check.
- The first build used `org.openmuc.j60870.InformationObject`; the class is in `org.openmuc.j60870.ie`, as the javadoc
  lists it. Fixed in `Station.java`.

## Decisions

- D-052: the peer, the first direction, and the open decision for X3b (how the planner reaches an external station).

## Open

- X3b: the other direction, the scenarios through the planner, and the peer's commands, reads, and spontaneous updates.
- The maintainer's review of the results and of the feature matrix, which the plan requires before X3 is done.

## Points for the next tasks

- Recommended for X3b: option (b) of D-052, a second bench that publishes its station through X2, so the planner keeps
  its current rule for external connections.

## Size

About 110 lines of station program and image, about 130 lines of test, and about 15 lines of CI.
