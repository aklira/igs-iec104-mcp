# V1a — TLS in the runtime, both roles

## Prompt

Task V1 of `IMPLEMENTATION.md` §14: TLS scenarios. Depends on C2, C3, and D2. Both roles with the upstream TLS
settings; acceptance and rejection cases. V1 is split (D-041): this entry is its first part, the runtime.

## Summary

- `src/tls.rs` (new): the upstream settings from three DER files (certificate, key, trust), validated against the profile
  before the OpenSSL backend is built. A missing or unusable file is a `TlsSetupError`.
- `src/config.rs`: `--tls-certificate`, `--tls-key`, `--tls-trust` (all or none), and `--tls-server-name` (a client only).
  `Config.tls` holds the files.
- `src/runtime.rs`: the files are read before any port is bound; a failure is `BindError::Tls`. A client's host is the
  server name, or its address.
- `src/client_role.rs`, `src/server_role.rs`: `client_config` and `bind` take the secure wrapper and apply it.
- `tests/tls.rs` (new): a secured client reaches and starts a secured station; a client that trusts another root is refused
  with a `connect_failed` naming the handshake; a missing key stops the start.
- D-041 records the split, the decisions, and what is not in V1a.

Dependencies: none new. The upstream `openssl` feature is the default of the library the runtime already depends on.

## Verification

- `cargo test --workspace --all-features` in `runtime/` passes, including `tests/tls.rs` (3 tests) and the new option
  tests.
- `cargo clippy --workspace --all-targets --all-features -- -D warnings` passes; `cargo fmt --all -- --check` passes;
  `cargo deny check` reports ok (the existing warning about an unused licence allowance is unchanged).
- `tests/integration/test_runtime_scenario.py` and `test_restart_scenario.py` pass with the rebuilt binary.

## Decisions

- D-041: the split of V1, the files and their rules, the failure to start, the client's host check, and what is not in
  V1a.

## Open

- The runtime's TLS security events are discarded. The journal does not yet say why a handshake failed beyond the
  backend's text. V1c decides whether they are recorded.
- A certificate chain longer than the leaf is not sent. A deployment with an intermediate needs a decision on how the
  chain is delivered (V1b).

## Points for the next tasks

- V1b: the certificate material reaches the containers through the archive interface, never through the environment, a
  bind mount, or the evidence (the rule of the control token). The bench definition names the files by `secret:<name>`
  references only.
- V1c: a scenario with a refused client, and the station's record of the refused connection.

## Size

About 206 lines changed in the runtime's existing files, and 459 lines in two new files (`src/tls.rs` and
`tests/tls.rs`), most of them the test fixtures that generate the certificates.
