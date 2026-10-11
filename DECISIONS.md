# Decisions

Each decision has an ID (`D-001`, ...), the date, the reasons, and the test that shows it, when a
test can show it.

## D-001: Python toolchain

- Date: 2026-10-11 (task B1).
- Decision: `uv` manages the environment and the lockfile (`uv.lock`, committed). The minimum
  Python is 3.12 (`requires-python = ">=3.12"`), and `.python-version` pins 3.12 for development and
  CI. The project is not packaged (`[tool.uv] package = false`): no build backend is needed yet,
  and adding one would add a dependency that §11 does not list. Packaging is decided when the
  project is distributed.
- Test: the gate of `AGENTS.md` runs with `uv sync --locked`, so the lockfile must match
  `pyproject.toml`.

## D-002: YAML library

- Date: 2026-10-11 (task B1).
- Decision: `ruamel.yaml` (MIT) reads definitions with its safe loader (`YAML(typ="safe")`).
- Reason: §11 allowed either `ruamel.yaml` or `PyYAML`. `ruamel.yaml` ships type information
  (`py.typed`), so mypy in strict mode needs no stub package. `PyYAML` has no type information, so
  it would need `types-PyYAML`, which §11 does not list.
- Test: `tests/unit/test_yaml_policy.py` checks that the safe loader refuses a tag that constructs a
  Python object.

## D-003: Lint and type-check settings

- Date: 2026-10-11 (task B1).
- Decision: ruff checks the rule families E, F, W, I, B, UP, SIM and RUF with a line length of 88.
  mypy runs in strict mode over `src`, `tests` and `scripts`. pytest collects `tests/unit` with
  `asyncio_mode = "auto"`.

## D-004: Upstream revision of `igs-iec104`

- Date: 2026-10-11 (task B2).
- Decision: the runtime depends on `igs-iec104` at revision `e91ecc824fcbc750262a5c8505c8812457d200c1`
  (commit `e91ecc8`), pinned by `rev` in `runtime/igs-iec104-runtime/Cargo.toml`. Cargo resolves the
  codec and link crates of that repository from the same revision.
- Reason: `e91ecc8` is the first revision with the upstream prerequisites U1 to U4 of
  `IMPLEMENTATION.md` §4. Its upstream CI run succeeded. The upstream repository is public, so CI
  and the image build fetch it without credentials.
- Change: a later revision is a new decision in this file, with the same test.
- Test: `tests/unit/test_runtime_pin.py` checks that the `rev` of the manifest is the one recorded
  here.

## D-005: Rust toolchain and base images

- Date: 2026-10-11 (task B2).
- Decision: `runtime/rust-toolchain.toml` pins the toolchain `1.99.0`. The minimum supported Rust
  version stays `1.85` (`rust-version` in `runtime/Cargo.toml`, the minimum of `IMPLEMENTATION.md`
  §11). The image builds on `rust:1.99.0-trixie` and runs on `debian:trixie-slim`.
- Open: the base images are pinned by tag, not by digest. The digests are recorded when CI
  publishes the image.
- Test: `tests/unit/test_runtime_pin.py` checks that the channel of the toolchain file is the one
  recorded here.

## D-006: TLS backend of the runtime

- Date: 2026-10-11 (task B2).
- Decision: the runtime uses the default TLS backend of `igs-iec104`, OpenSSL (its `openssl`
  feature), and not `rustls`.
- Reason: the upstream documentation says the `rustls` backend lacks mandatory items of the
  IEC 62351-3 profile (CBC, DHE and CCM cipher suites, RSA-PSS, and TLS 1.2 renegotiation). The
  build needs the OpenSSL development files (`libssl-dev`), and the image runs with `libssl3t64`.
- Current state: the binary does not call OpenSSL yet, so the linker drops `libssl` from it
  (checked with `ldd` in the image, built with Podman). The runtime image installs `libssl3t64`
  already, as §11 requires; the library becomes needed when S1 and S2 use TLS.
- Test: none in B2.

## D-007: Licences admitted for transitive and development dependencies

- Date: 2026-10-11 (task B1 finding, decided by the maintainer for B1 and B2).
- Decision: in addition to MIT, Apache-2.0, BSD, and ISC, the policy of `IMPLEMENTATION.md` §2
  admits MPL-2.0, PSF-2.0, and MIT-0. They are admitted only for transitive and development
  dependencies that are used unmodified. A direct dependency under one of these licences still needs
  approval.
- Reason: four installed distributions that approved dependencies pull in use these licences:
  `certifi` (MPL-2.0, through `httpx`), `typing_extensions` (PSF-2.0, through `anyio`, `httpx`,
  and `pydantic`), `cffi` (MIT-0, through `cryptography` and `mcp[crypto]`), and `pathspec`
  (MPL-2.0, through `mypy`, development only). Removing them would mean removing approved
  dependencies.
- Enforcement: `scripts/check_licences.py` checks the installed distributions, and CI runs it.
  `tests/unit/test_licence_policy.py` checks the admitted and refused expressions.
- Scope: the Rust runtime keeps the list of its `deny.toml`, which already matches §2.
