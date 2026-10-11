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

## Q-002: point kinds of the definitions and of the runtime

- Task: C4. Status: open, for the maintainer.
- Question: the definitions accept the eleven monitoring kinds of the upstream `PointValue` (§5), but the
  runtime serves five of them today (single, double, normalized, scaled, and float). A bench that uses
  step, bitstring, counter, packed, or normalized-unqualified points is valid, and fails when the runtime
  is asked to serve it. Should the definitions accept only the kinds the runtime serves until each is
  implemented, or accept all eleven now, with the runtime refusing the rest by name at start?
- Effect until decided: the definitions accept all eleven. The runtime refuses the others when it starts
  (task D2 for the bench lifecycle, where a definition is turned into runtime configuration).
