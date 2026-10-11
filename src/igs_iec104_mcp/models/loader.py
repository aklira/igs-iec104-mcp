"""Reads the definitions from YAML, safely, and reports every problem with its path
(IMPLEMENTATION.md §5).

The YAML is read with the safe loader of `ruamel.yaml`: a tag that would build an object
(`!!python/...`) is refused, and so is a key given twice. The document is then validated
by its model, which refuses every field it does not name."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from igs_iec104_mcp.models.bench import TestBench
from igs_iec104_mcp.models.common import DefinitionError, Issue
from igs_iec104_mcp.models.run import Run
from igs_iec104_mcp.models.scenario import Scenario


def load_document(text: str) -> Any:
    """
    The data of a YAML document, read with the safe loader. Raises DefinitionError when
        it is not valid YAML.
    """
    yaml = YAML(typ="safe", pure=True)
    yaml.allow_duplicate_keys = False
    try:
        return yaml.load(text)
    except YAMLError as error:
        raise DefinitionError(
            [Issue("", f"not valid YAML: {_first_line(error)}")]
        ) from error


def parse[M: BaseModel](model: type[M], text: str) -> M:
    """The `model` definition of a YAML document. Raises DefinitionError listing "
    "every problem."""
    data = load_document(text)
    if not isinstance(data, dict):
        raise DefinitionError(
            [Issue("", "a definition is a mapping of fields, not a list or a value")]
        )
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise DefinitionError([_issue(item) for item in error.errors()]) from error


def load_bench(text: str) -> TestBench:
    return parse(TestBench, text)


def load_scenario(text: str) -> Scenario:
    return parse(Scenario, text)


def load_run(text: str) -> Run:
    return parse(Run, text)


def _issue(item: Any) -> Issue:
    path = ".".join(str(part) for part in item.get("loc", ()))
    message = str(item.get("msg", "invalid"))
    message = message.removeprefix("Value error, ")
    if item.get("type") == "extra_forbidden":
        message = "is not a field of this definition: remove it, or check its spelling"
    elif item.get("type") == "missing":
        message = "is required"
    return Issue(path, message)


def _first_line(error: Exception) -> str:
    return (
        str(error).strip().splitlines()[0]
        if str(error).strip()
        else type(error).__name__
    )
