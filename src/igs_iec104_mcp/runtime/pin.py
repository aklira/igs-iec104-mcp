"""The upstream revision the runtime is built against (DECISIONS.md D-004).

It must equal the pin in `runtime/igs-iec104-runtime/Cargo.toml`, which
tests/unit/test_runtime_pin.py checks. A run records it with the image identities, so a
report says which library a run used.
"""

UPSTREAM_REVISION: str = "e91ecc824fcbc750262a5c8505c8812457d200c1"
