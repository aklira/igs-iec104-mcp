# Issue #1 — Agent-friendly installation: split into tasks

## Prompt

The maintainer asked for a review of issue #1 (`aklira/igs-iec104-mcp#1`), then answered the questions raised:
Linux and WSL2 as hosts; Podman first; Claude Code and Codex for v1, with the generic mode marked as still to implement.
The maintainer then asked to split the issue into tasks in `IMPLEMENTATION.md`.

## Summary

- `IMPLEMENTATION.md`: Phase I (tasks I1 to I9) added to §14, before Phase V; rows I1 to I9 added to §16, unchecked.
- `DECISIONS.md`: D-056, the maintainer's answers and the design choices (bash installer, Python doctor, no privileged steps).
- `docs/ai-log/QUESTIONS.md`: Q-004, the write policy of the client registration, open and gating I6.

No code changed. No commit made. The phase's dependencies on existing tasks (B2, M1, M2, D2) are all done, so Phase I
can start at I1.

## Verification

- Every task of Phase I names its dependencies, its output and its done criterion, and §16 lists the same nine IDs.
- The existing references were read from the files: `README.md`, `docs/platform-matrix.md`, `docs/setup.md`,
  `examples/benches/basic-pair.yaml`, `scripts/` (only `check_licences.py`), and the absence of a console script (D-047).
