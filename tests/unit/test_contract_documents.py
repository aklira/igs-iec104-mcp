"""The contract documents are valid, and they name the same version (task C1)."""

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from openapi_spec_validator import validate

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
API_FILE = "runtime-api.yaml"
EVENTS_FILE = "events.schema.json"
STATION_EVENTS_FILE = "station-events.schema.json"


def test_the_openapi_document_is_valid(contract: Any) -> None:
    path = (CONTRACTS / API_FILE).resolve()
    validate(contract.api, base_uri=path.as_uri())


def test_the_events_schema_is_valid(contract: Any) -> None:
    Draft202012Validator.check_schema(contract.events)


def test_the_station_events_schema_is_valid_and_names_the_version(
    contract: Any,
) -> None:
    station = json.loads((CONTRACTS / STATION_EVENTS_FILE).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(station)
    version = contract.api["info"]["version"]
    assert f"contract version {version}" in station["$comment"]
    assert len(station["$defs"]["StationEvent"]["oneOf"]) == 5


def test_both_documents_name_the_same_contract_version(contract: Any) -> None:
    version = contract.api["info"]["version"]
    assert contract.api["components"]["schemas"]["ContractVersion"]["const"] == version
    assert f"contract version {version}" in contract.events["$comment"]


def test_every_event_kind_is_in_the_schema(contract: Any) -> None:
    kinds = {
        choice["$ref"].rsplit("/", 1)[-1]
        for choice in contract.events["$defs"]["Event"]["oneOf"]
    }
    assert len(kinds) == 11
    assert {"Asdu", "Closed", "Transfer"} <= kinds


def test_the_events_file_is_a_json_schema() -> None:
    schema = json.loads((CONTRACTS / EVENTS_FILE).read_text(encoding="utf-8"))
    assert schema["$schema"].startswith("https://json-schema.org/")
