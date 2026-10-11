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
