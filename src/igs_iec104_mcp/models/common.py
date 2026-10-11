"""Shared types of the definitions: names, addresses, durations, secret references, and
errors.

The ranges come from IMPLEMENTATION.md §5 and from the upstream library: the common
address from docs/igs104-server.md, and the link parameters from `igs-iec104-link`
(config.rs). A definition is read strictly: an unknown field is an error, and every
error names the field and says what is allowed."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict

SCHEMA_VERSION: int = 1
# The type of the schema_version field of every top-level definition.
SchemaVersion = Literal[1]

# The common address of a station: 1 to 65534. 65535 is the global address (IEC
# 60870-5-104 §8.1).
MIN_COMMON_ADDRESS: int = 1
MAX_COMMON_ADDRESS: int = 65534
# An information object address is three octets: 1 to 16777215 (docs/igs104-server.md).
MIN_ADDRESS: int = 1
MAX_ADDRESS: int = 16_777_215

# A name of a bench, an instance, a target, a network, a scenario, or a step: lower-
# case, short.
NAME_PATTERN: str = r"^[a-z][a-z0-9-]{0,62}$"
# A reference to a secret: the controller keeps the secret, the definition names it.
SECRET_PATTERN: str = r"^secret:[a-z][a-z0-9_.-]{0,126}$"
_NAME = re.compile(NAME_PATTERN)
_SECRET = re.compile(SECRET_PATTERN)
_DURATION = re.compile(r"^([0-9]+)(ms|s|m|h)$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
# The longest a step may wait, and the longest a whole scenario may run.
MAX_STEP_SECONDS: float = 3600.0
MAX_SCENARIO_SECONDS: float = 86_400.0


class DefinitionError(Exception):
    """
    A definition is not valid. `issues` lists each problem, with its path in the
    document.
    """

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        super().__init__("; ".join(str(issue) for issue in issues))


@dataclass(frozen=True)
class Issue:
    """One problem of a definition: where it is, and what to change."""

    path: str
    message: str

    def __str__(self) -> str:
        where = self.path or "(the document)"
        return f"{where}: {self.message}"


class Definition(BaseModel):
    """
    The base of every definition: no field is accepted that the model does not name.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


def check_name(value: str) -> str:
    if not _NAME.fullmatch(value):
        raise ValueError(
            f"{value!r} is not a valid name: use 1 to 63 lower-case letters, digits, "
            f"or '-', "
            "starting with a letter"
        )
    return value


def check_secret(value: str) -> str:
    if not _SECRET.fullmatch(value):
        raise ValueError(
            f"{value!r} is not a secret reference: write it as 'secret:<name>', and "
            f"keep the key "
            "or the password in the controller's secret store, never in the definition"
        )
    return value


def check_common_address(value: int) -> int:
    if not MIN_COMMON_ADDRESS <= value <= MAX_COMMON_ADDRESS:
        raise ValueError(
            f"common address {value} is out of range: use {MIN_COMMON_ADDRESS} to "
            f"{MAX_COMMON_ADDRESS} (65535 is the global address and is not a station's)"
        )
    return value


def check_address(value: int) -> int:
    if not MIN_ADDRESS <= value <= MAX_ADDRESS:
        raise ValueError(
            f"information object address {value} is out of range: use {MIN_ADDRESS} to "
            f"{MAX_ADDRESS} (three octets)"
        )
    return value


def check_timing(value: str) -> str:
    """
    A duration such as `500ms`, `10s`, `5m`, or `1h`, above zero and at most an hour.
    """
    seconds = duration_seconds(value)
    if seconds <= 0:
        raise ValueError(f"{value!r} must be a duration above zero, such as '10s'")
    if seconds > MAX_STEP_SECONDS:
        raise ValueError(f"{value!r} is longer than an hour: split the wait into steps")
    return value


def duration_seconds(value: str) -> float:
    """The number of seconds of a duration such as `500ms` or `10s`. Raises ValueError
    otherwise."""
    match = _DURATION.fullmatch(value)
    if match is None:
        raise ValueError(
            f"{value!r} is not a duration: write a number and a unit, such as "
            f"'500ms', '10s', or '5m'"
        )
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


CommonAddress = Annotated[int, AfterValidator(check_common_address)]
Address = Annotated[int, AfterValidator(check_address)]
Name = Annotated[str, AfterValidator(check_name)]
SecretRef = Annotated[str, AfterValidator(check_secret)]
Timing = Annotated[str, AfterValidator(check_timing)]


def check_global_timing(value: str) -> str:
    """A duration for a whole scenario: above zero and at most a day."""
    seconds = duration_seconds(value)
    if seconds <= 0:
        raise ValueError(f"{value!r} must be a duration above zero, such as '10m'")
    if seconds > MAX_SCENARIO_SECONDS:
        raise ValueError(f"{value!r} is longer than a day: split the scenario")
    return value


GlobalTiming = Annotated[str, AfterValidator(check_global_timing)]


def canonical_json(value: Any) -> str:
    """
    The JSON text of a value, with sorted keys and no spaces: a value always gives the
        same text.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_of(value: Any) -> str:
    """The SHA-256 of the canonical JSON of a value."""
    return hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()
