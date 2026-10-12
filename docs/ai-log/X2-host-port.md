# X2 follow-up — The engine chooses the host port of a published station

## Prompt

The maintainer approved option A of `docs/decisions/host-port.md` on 2026-10-12, and the recommended answers to its three
questions. This is the change D-054 records.

## Summary

- `host_port: 0` means the engine's choice, in the model, the spec, and the adapter. The planner does not count `0` as a
  collision.
- The bench reads the bound port from the engine after the publication container starts, and builds the advertised
  endpoint from it.
- A run records a `published` event per published station, with the endpoint, when its bench starts.
- The X2 test uses `0`; it no longer picks a free port before the start.
- The bench schema is regenerated. D-054 records the decision.

Dependencies: none added.

## Verification

- The X2 publication test passes on Podman with `host_port: 0`: the advertised endpoint names the declared address and the
  port the engine bound; a runtime started on the host connects to it, starts its transfer, interrogates the station, and
  reads the value.
- The unit suite passes (479 tests), with ruff, ruff format, and mypy strict.
- The full integration suite passes after the change: 40 tests, none skipped, on Podman.

## Problems found

- The unit test that refused `host_port: 0` had to change: `0` is now the engine's choice, so the refused value is `-1`.
- The first draft of the runner test appended YAML to the basic pair's text, which put the publication under the wrong key.
  The test now sets the field on the loaded document, as the other tests do.

## Decisions

- D-054: the engine chooses the host port; the endpoint is in the run journal; the X2 test moves to `0`.

## Open

- `run_get` does not show a run's published endpoints yet; the journal has them.
- The external client outside Docker, with the independent peer, is not tested yet with an engine-chosen port: the X2 test
  covers a runtime on the host, which is the external path the plan names.

## Size

About 60 lines of source, and about 70 lines of tests and documentation.
