"""The result of a preflight check: its status, what was found, and the corrective step
when it did not pass (IMPLEMENTATION.md §14, tasks I2 and I3; issue #1)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    """OK passes. WARNING does not stop the installation. FAIL is a required check
    that did not pass."""

    OK = "ok"
    WARNING = "warning"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    action: str = ""
    data: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Report:
    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        return all(check.status is not Status.FAIL for check in self.checks)

    def as_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status.value,
                    "detail": check.detail,
                    "action": check.action,
                    "data": dict(check.data),
                }
                for check in self.checks
            ],
        }
