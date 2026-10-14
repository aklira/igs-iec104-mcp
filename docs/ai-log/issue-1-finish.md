# Issue #1 — Finishing batch

## Prompt

After the review of the issue's acceptance criteria (the answer given before this batch), the maintainer asked for the
finishing batch: the image identities in the preflight, the platform matrix (the installer, the client versions, the
fresh-checkout job), and a real Codex session.

## Summary

- `src/igs_iec104_mcp/doctor.py`: the `images` check (D-062). It reads the installation's images from the state file, or
  from `--runtime-image` and `--relay-image`, and asks the engine for each identity. A missing image is a WARNING, because
  the installer runs the preflight before it builds the images. `Probe.image_exists` is the new method of the probe.
  `--runtime-image` and `--relay-image` now serve the preflight as well as `--run-bench`.
- `tests/unit/test_doctor.py`: six tests for the check. The tests no longer read the machine's state file: the helper
  points at a missing file, and the machine fixture sets `XDG_STATE_HOME`.
- `docs/platform-matrix.md`: the installer on WSL2 with Podman, the fresh-checkout job, the Claude Code and Codex CLI
  versions, the uv version, the installer and registration rows, the doctor row, the Codex session row, and the release
  gate text.
- `docs/install-agent.md`: the `images` line of the level 1 check, and the Codex approval note of step 7.
- `DECISIONS.md`: D-062 and D-063.

## Real checks

- Podman 5.7.0 (WSL2), with the installer's state: `images ok`. With an absent identity: `images warn`, with the
  installer's step, and exit 0.
- Codex CLI 0.147.0, a session with `--ephemeral --ignore-user-config`, the server given by a `-c` override, so no
  configuration file was written: the server connected, `bench_validate` returned `valid: true`. Two earlier sessions in
  the same setup were cancelled by Codex, because the tool calls of an MCP server need an approval (D-063). The session
  used the maintainer's Codex account, and it made model calls.

## Verification

- Python gate: `ruff format --check`, `ruff check`, `mypy` (strict, 115 files), `pytest tests/unit` (542 passed).
- Integration: `test_install.py` and `test_doctor_handshake.py` (11 passed).
