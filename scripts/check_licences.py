"""Check the licences of the installed distributions against IMPLEMENTATION.md §2.

Run it in the project environment: `uv run python scripts/check_licences.py`.
It exits with status 1 when a distribution has a licence outside the policy or
none that can be read. It uses only the standard library.
"""

from __future__ import annotations

import importlib.metadata as metadata
import sys

# The licences the policy of §2 allows, matched exactly or by prefix.
# MPL-2.0, PSF-2.0 and MIT-0 are admitted by decision D-007: transitive and development
# dependencies used unmodified.
ALLOWED_EXACT = frozenset(
    {"MIT", "Apache-2.0", "ISC", "BSD", "MPL-2.0", "PSF-2.0", "MIT-0"}
)
ALLOWED_PREFIX = ("BSD-",)

# Trove classifiers, used only when a distribution states no licence expression.
CLASSIFIERS = {
    "MIT License": "MIT",
    "Apache Software License": "Apache-2.0",
    "ISC License (ISCL)": "ISC",
    "BSD License": "BSD",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Python Software Foundation License": "PSF-2.0",
}


def licence_of(meta: metadata.PackageMetadata) -> str | None:
    expression = meta.get("License-Expression")
    if expression:
        return expression.strip()
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License :: "):
            name = classifier.rsplit(" :: ", 1)[-1]
            if name in CLASSIFIERS:
                return CLASSIFIERS[name]
    legacy = meta.get("License")
    if legacy and legacy.strip() not in {"", "UNKNOWN"}:
        return legacy.strip()
    return None


def is_allowed(expression: str) -> bool:
    """An `OR` expression is allowed when one alternative is allowed."""
    for alternative in expression.split(" OR "):
        term = alternative.strip().strip("()")
        if term in ALLOWED_EXACT or term.startswith(ALLOWED_PREFIX):
            return True
    return False


def main() -> int:
    seen: dict[str, tuple[str, str | None]] = {}
    for distribution in metadata.distributions():
        meta = distribution.metadata
        name = meta["Name"]
        if name not in seen:
            seen[name] = (distribution.version, licence_of(meta))
    ordered = sorted(seen.items(), key=lambda item: item[0].lower())
    violations = [
        (name, version, licence)
        for name, (version, licence) in ordered
        if licence is None or not is_allowed(licence)
    ]
    for name, version, licence in violations:
        print(f"{name} {version}: licence {licence or 'unknown'} is outside the policy")
    print(f"checked {len(seen)} distributions, {len(violations)} outside the policy")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
