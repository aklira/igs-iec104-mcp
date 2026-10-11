"""Fixtures shared by the tests: the contracts, and the runtime binary."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
API_FILE = "runtime-api.yaml"
EVENTS_FILE = "events.schema.json"
STATION_EVENTS_FILE = "station-events.schema.json"


@dataclass(frozen=True)
class Contract:
    """The contract documents, and a validator that resolves their references."""

    api: dict[str, Any]
    events: dict[str, Any]
    registry: Registry

    def validator(self, schema: dict[str, Any]) -> Draft202012Validator:
        return Draft202012Validator(schema, registry=self.registry)

    def component(self, name: str) -> dict[str, Any]:
        """A schema of `components/schemas`, referenced from the API document."""
        return {"$ref": f"{API_FILE}#/components/schemas/{name}"}

    def response_schema(
        self, template: str, method: str, status: int
    ) -> dict[str, Any]:
        """The schema of the JSON body of one documented response."""
        operation = self.api["paths"][template][method.lower()]
        response = operation["responses"][str(status)]
        if "$ref" in response:
            name = response["$ref"].rsplit("/", 1)[-1]
            response = self.api["components"]["responses"][name]
        schema: dict[str, Any] = response["content"]["application/json"]["schema"]
        resolved: dict[str, Any] = _resolve_in_api(schema)
        return resolved

    def documents_status(self, template: str, method: str, status: int) -> bool:
        return str(status) in self.api["paths"][template][method.lower()]["responses"]


def _resolve_in_api(node: Any) -> Any:
    """Points the references of the API document at its file, so that they resolve."""
    if isinstance(node, dict):
        return {
            key: (
                f"{API_FILE}{value}"
                if key == "$ref" and isinstance(value, str) and value.startswith("#/")
                else _resolve_in_api(value)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_resolve_in_api(item) for item in node]
    return node


@pytest.fixture(scope="session")
def contract() -> Contract:
    api_text = (CONTRACTS / API_FILE).read_text(encoding="utf-8")
    api = YAML(typ="safe").load(api_text)
    events = json.loads((CONTRACTS / EVENTS_FILE).read_text(encoding="utf-8"))
    station_events = json.loads(
        (CONTRACTS / STATION_EVENTS_FILE).read_text(encoding="utf-8")
    )
    registry = Registry().with_resources(
        [
            (API_FILE, Resource.from_contents(api, default_specification=DRAFT202012)),
            (
                EVENTS_FILE,
                Resource.from_contents(events, default_specification=DRAFT202012),
            ),
            (
                STATION_EVENTS_FILE,
                Resource.from_contents(
                    station_events, default_specification=DRAFT202012
                ),
            ),
        ]
    )
    return Contract(api=api, events=events, registry=registry)


@pytest.fixture
def runtime_binary() -> Path:
    """The runtime binary named by IGS_RUNTIME_BINARY.

    Tests skip without it, but in CI they fail: a missing binary must not pass.
    """
    value = os.environ.get("IGS_RUNTIME_BINARY")
    if not value:
        message = "set IGS_RUNTIME_BINARY to the igs-iec104-runtime binary"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    path = Path(value)
    assert path.is_file(), f"{path} is not a file"
    return path
