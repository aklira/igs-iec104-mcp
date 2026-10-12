# V1b — TLS material of a bench from a secrets directory

## Prompt

Task V1 of `IMPLEMENTATION.md` §14, second part (D-041): the controller delivers the certificate material through the
archive interface, and the bench definition names it by secret references. The operator's choices were: a secrets
directory given to the server, the intermediates as a repeatable option, and DER files from the operator.

## Summary

- `runtime/igs-iec104-runtime`: `--tls-intermediate FILE` (repeatable) builds the chain after the certificate; the
  option rules are in `config.rs`, and the chain in `tls.rs`.
- `definitions/secrets.py` (new): `SecretStore` reads a secret from an owner-only directory outside the project,
  refusing a link, a file readable by others, and one over 64 KiB; the errors never show the content.
- `models/instance.py`: `InstanceTls` (certificate, key, trust, up to eight intermediates, and a client's server name)
  on an instance, with secret references only.
- `docker/plan.py`: the TLS flags of a runtime with its files in the run directory; the file names each reference is
  delivered as.
- `docker/bench.py`: the material is checked before any resource is created, and delivered by `put_files` with the
  points and the token.
- `scenarios/runner.py`: the content is read when a run starts, a `secrets` event records the digests, and the engine
  receives the material. `server.py`: `--secrets-dir`. `reporting/reports.py`: the digests in the inputs.
- D-042 records the decisions.

Dependencies: none new.

## Verification

- `tests/integration/test_docker_tls.py` passes on Podman with the runtime image rebuilt from this repository: the
  secured bench starts, the client's TLS connection comes up, the client interrogates the station and reads 12.5, and
  the bench is removed.
- The Docker regression tests pass with the new image: `test_mcp_run.py`, `test_docker_publish.py`,
  `test_mcp_report.py`, and `test_docker_reconcile.py` (4 tests).
- The unit suite and the MCP stdio test pass (417 tests), with the new tests for the store, the definition, the plan,
  and the run.
- The runtime's workspace tests pass (`tests/tls.rs`: 5 tests), with clippy, format, and `cargo deny`.
- Gates: `ruff format --check`, `ruff check`, `mypy` (strict, 90 source files), and the schema check.

## Decisions

- D-042: the secrets directory and its rules, the delivery, the definition, the intermediates, and the plan.

## Open

- The external targets' TLS names are not delivered yet. Their secrets are in the definition but not in any run.
- A rotation of a secret during a bench's life is not supported: a bench reads its material when it starts.

## Points for the next tasks

- V1c: the acceptance and rejection scenarios. A refused client is observed through the `connect_failed` event; the
  station's record of the refused connection is a station event to check.
- The runtime's security events (the Annex A ones) are still discarded; V1c decides whether a refusal records its reason.

## Size

About 234 lines added to existing Python files and 79 in the new store module; about 188 lines added to the runtime's
existing files, the new option and its tests; and about 640 lines of new tests, most of them the store, the definition,
and the Docker test's certificates.
