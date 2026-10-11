"""The published schemas are the models' schemas: a change to one of them is caught."""

import json
from pathlib import Path

from igs_iec104_mcp.models import schemas

COMMITTED = Path(__file__).resolve().parents[2] / "contracts" / "definitions"


def test_the_committed_schemas_match_the_models() -> None:
    assert schemas.stale(COMMITTED) == []


def test_a_schema_that_differs_from_its_model_is_reported(tmp_path: Path) -> None:
    schemas.publish(tmp_path)
    assert schemas.stale(tmp_path) == []
    edited = json.loads((tmp_path / "bench.schema.json").read_text(encoding="utf-8"))
    edited["title"] = "an edit by hand"
    (tmp_path / "bench.schema.json").write_text(json.dumps(edited), encoding="utf-8")
    assert schemas.stale(tmp_path) == ["bench"]


def test_a_missing_schema_is_reported(tmp_path: Path) -> None:
    assert sorted(schemas.stale(tmp_path)) == ["bench", "run", "scenario"]


def test_the_check_command_fails_on_drift(tmp_path: Path) -> None:
    assert schemas.main(["--check", "--directory", str(tmp_path)]) == 1
    schemas.publish(tmp_path)
    assert schemas.main(["--check", "--directory", str(tmp_path)]) == 0
