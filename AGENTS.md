# AGENTS.md

Python MCP server that orchestrates IEC 60870-5-104 test benches built on the Rust library
`igs-iec104`. The plan of record is `IMPLEMENTATION.md`; this file summarises its working rules
(§2) for agents.

## Process

- Read `IMPLEMENTATION.md` before any task. Read only the inputs that the task lists.
- One task at a time, in dependency order (§14). Keep a change under about 400 lines; split a
  larger task.
- Commit only when the maintainer asks; push only when the maintainer asks.
- Commit message: `<area>: <what>`, a blank line, then `Task: <ID>`. Tick the task's box in §16 in
  the commit where the task is done, and only then.
- Tasks marked ⛔ are human gates: stop and ask a maintainer.
- Record each task's prompt and summary in `docs/ai-log/<task-id>.md`. Record open questions in
  `docs/ai-log/QUESTIONS.md` and resolved decisions in `DECISIONS.md`.
- Everything committed is in English.

## Legal and clean room

- Never copy IEC standard text into code, comments, docs, tests, YAML examples, or commit messages.
  Cite clauses (for example `IEC 60870-5-104 §7.5`) and paraphrase. Type identifications, causes,
  field sizes and bit positions are data and may be used.
- Never open, quote, reconstruct or describe the source of other IEC 104 implementations. They are
  used as black boxes only: a separate process or container, driven over the network.
- Dependencies must be MIT, Apache-2.0, BSD or ISC. Decision D-007 also admits MPL-2.0, PSF-2.0
  and MIT-0 for transitive and development dependencies used unmodified. Ask before adding a
  dependency that §11 does not list.
- Secrets (control credentials, private keys, passwords) are never committed, logged or written
  into reports.

## Standard gate

```bash
uv run ruff format --check
uv run ruff check
uv run mypy
uv run pytest tests/unit
```

The integration tests build the runtime first (`cargo build --locked --package igs-iec104-runtime`
in `runtime/`) and set `IGS_RUNTIME_BINARY` to the binary; they run with
`uv run pytest tests/integration`.

Run `uv sync --locked` first. The Rust runtime has its own gate, in `runtime/`:

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
cargo deny check
```

## Layout

The package is `src/igs_iec104_mcp/`; tests are in `tests/unit`, `tests/integration` and
`tests/e2e`. `scripts/check_licences.py` checks the licences of the installed packages.
