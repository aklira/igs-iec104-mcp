"""The published JSON Schemas of the definitions, generated from the models
(IMPLEMENTATION.md §5, §15).

`contracts/definitions/` holds the schemas. They are written from the models with
`--write`, and `--check` fails when a committed file differs from what the models give,
so that the schemas cannot drift."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel

from igs_iec104_mcp.models.bench import TestBench
from igs_iec104_mcp.models.run import Run
from igs_iec104_mcp.models.scenario import Scenario

DEFINITIONS: dict[str, type[BaseModel]] = {
    "bench": TestBench,
    "scenario": Scenario,
    "run": Run,
}
DEFAULT_DIRECTORY = Path(__file__).resolve().parents[3] / "contracts" / "definitions"


def render(model: type[BaseModel]) -> str:
    """The JSON Schema of a model, as the file holds it."""
    schema = model.model_json_schema(mode="validation")
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def file_name(name: str) -> str:
    return f"{name}.schema.json"


def publish(directory: Path = DEFAULT_DIRECTORY) -> list[Path]:
    """Writes the schemas of all the definitions into `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, model in DEFINITIONS.items():
        path = directory / file_name(name)
        path.write_text(render(model), encoding="utf-8")
        written.append(path)
    return written


def stale(directory: Path = DEFAULT_DIRECTORY) -> list[str]:
    """The names of the schemas that are missing or differ from what the models give."""
    differing = []
    for name, model in DEFINITIONS.items():
        path = directory / file_name(name)
        if not path.is_file() or path.read_text(encoding="utf-8") != render(model):
            differing.append(name)
    return differing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish or check the definition schemas."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--write", action="store_true", help="write the schemas from the models"
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="fail when a schema differs from the models",
    )
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    arguments = parser.parse_args(argv)
    if arguments.write:
        for path in publish(arguments.directory):
            print(f"wrote {path}")
        return 0
    differing = stale(arguments.directory)
    if differing:
        print(
            (
                f"the schemas differ from the models: {', '.join(differing)}; run with "
                f"--write"
            ),
            file=sys.stderr,
        )
        return 1
    print("the schemas match the models")
    return 0


if __name__ == "__main__":
    sys.exit(main())
