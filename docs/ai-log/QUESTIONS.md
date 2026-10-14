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

- Task: C4. Status: resolved by D-019 (the maintainer chose the ten kinds, with the runtime to follow).
- Question: the definitions accept the ten monitoring kinds of the upstream `PointValue` (§5), but the
  runtime serves five of them today (single, double, normalized, scaled, and float). A bench that uses
  step, bitstring, counter, packed, or normalized-unqualified points is valid, and fails when the runtime
  is asked to serve it. Should the definitions accept only the kinds the runtime serves until each is
  implemented, or accept all ten now, with the runtime refusing the rest by name at start?
- Effect until decided: the definitions accept all ten. Since D2, the plan of a bench refuses the five
  kinds the runtime does not serve, by name, before any resource is created (`docker/plan.py`); the question
  of which of the two behaviours to keep remains open.

## Q-003: link parameters of the runtimes

- Task: D2. Status: resolved by D-020 (the maintainer chose flags on the runtime, as a separate task).
- Question: the definitions carry the link parameters of each instance (t0 to t3, k, w), but the runtime takes
  no flag for them: it uses the upstream defaults (§9.6). Should the runtime gain flags for the link
  parameters (a change of the runtime and of its contract, so a separate task), or should a bench keep to
  the defaults until then?
- Effect until decided: a bench whose instance sets a link parameter other than its default is refused at
  plan time, naming the parameters (`docker/plan.py`). The defaults of the model equal the upstream defaults
  (`igs-iec104-link`, `Parameters::default`).

## Q-004: write policy of the client registration

- Task: I6. Status: resolved by D-061 (the maintainer validated the recommendations on 2026-10-14).
- Question: should `scripts/install.sh --client` register the server in the user scope or in the project scope? When a
  different registration already exists, should it keep it, replace it with `--force`, or refuse? Should `--yes` write without
  showing the exact command first?
- Effect until decided: I6 does not start. I1 to I5 do not depend on it, and I7 to I9 wait for I6.

