# Open questions

## Q-001: licence policy and transitive dependencies

- Task: B1. Status: open, for the maintainer.
- Question: §2 allows MIT, Apache-2.0, BSD and ISC. Four installed distributions that the approved
  dependencies pull in are outside that list:

  | Distribution | Licence | Pulled in by |
  | --- | --- | --- |
  | `certifi` | MPL-2.0 | `httpx` (approved) |
  | `typing_extensions` | PSF-2.0 | `anyio`, `httpx`, `pydantic` |
  | `cffi` | MIT-0 | `cryptography`, through `mcp[crypto]` |
  | `pathspec` | MPL-2.0 | `mypy` (dev only) |

- Options: allow these licences in §2 with a decision recorded in `DECISIONS.md`, which the
  maintainer should review; or find other ways to get the same functionality (for example, drop the
  `crypto` extra of `mcp` if the runtime does not need it).
- Effect until decided: `scripts/check_licences.py` reports these four and exits with status 1. It
  is not part of the CI gate yet; it is added to CI when the policy is decided.
