# Open questions

## Q-001: licence policy and transitive dependencies

- Task: B1. Status: resolved by D-007 (the maintainer chose to amend §2).
- Question: §2 allows MIT, Apache-2.0, BSD and ISC. Four installed distributions that the approved
  dependencies pull in are outside that list:

  | Distribution | Licence | Pulled in by |
  | --- | --- | --- |
  | `certifi` | MPL-2.0 | `httpx` (approved) |
  | `typing_extensions` | PSF-2.0 | `anyio`, `httpx`, `pydantic` |
  | `cffi` | MIT-0 | `cryptography`, through `mcp[crypto]` |
  | `pathspec` | MPL-2.0 | `mypy` (dev only) |

- Decision: amend §2 to admit MPL-2.0, PSF-2.0 and MIT-0 for transitive and development
  dependencies used unmodified (D-007). The licence check runs in CI.
