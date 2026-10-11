"""The basic bench as data, shared by the model tests."""

from typing import Any

BENCH: dict[str, Any] = {
    "schema_version": 1,
    "name": "basic-pair",
    "networks": [{"name": "lan"}, {"name": "wan", "external": True}],
    "instances": [
        {"name": "client-1", "role": "client", "network": "lan", "common_address": 1},
        {
            "name": "server-1",
            "role": "server",
            "network": "lan",
            "common_address": 1,
            "points": [{"address": 100, "kind": "float", "value": 12.5}],
        },
    ],
    "connections": [{"client": "client-1", "server": "server-1"}],
}
