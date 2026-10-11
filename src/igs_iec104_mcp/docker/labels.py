"""The labels that mark a Docker resource as managed by this controller, and the owner a
resource is checked against before it is touched (IMPLEMENTATION.md §6).

A resource belongs to one run of one test bench of one project. Its labels say so, and
every removal checks the labels again after the lookup: a resource found by name alone
is never removed."""

from __future__ import annotations

import re
from dataclasses import dataclass

from igs_iec104_mcp.models.common import check_name

LABEL_NAMESPACE = "dev.igs-iec104.mcp"
MANAGED = f"{LABEL_NAMESPACE}.managed"
PROJECT = f"{LABEL_NAMESPACE}.project"
BENCH = f"{LABEL_NAMESPACE}.bench"
RUN = f"{LABEL_NAMESPACE}.run"
INSTANCE = f"{LABEL_NAMESPACE}.instance"
RUN_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class OwnershipError(ValueError):
    """An owner is not valid: a name is not a name, or a run is not a run identity."""


@dataclass(frozen=True)
class Ownership:
    """The project, the test bench, and the run that own a set of resources."""

    project: str
    bench: str
    run: str

    def __post_init__(self) -> None:
        for name in (self.project, self.bench):
            try:
                check_name(name)
            except ValueError as error:
                raise OwnershipError(str(error)) from error
        if not RUN_PATTERN.fullmatch(self.run):
            raise OwnershipError(
                f"{self.run!r} is not a run identity: it has 32 lower-case hex digits"
            )

    def labels(self, instance: str | None = None) -> dict[str, str]:
        """The labels a resource of this owner carries; a container also names its
        instance."""
        labels = {
            MANAGED: "true",
            PROJECT: self.project,
            BENCH: self.bench,
            RUN: self.run,
        }
        if instance is not None:
            labels[INSTANCE] = instance
        return labels

    def filters(self) -> dict[str, list[str]]:
        """The Docker filters that select the resources of this owner. The engine
        matches them, and `owns` checks them again."""
        return {"label": [f"{key}={value}" for key, value in self.labels().items()]}


def owns(labels: dict[str, str] | None, ownership: Ownership) -> bool:
    """Whether a resource with these labels belongs to the owner, exactly."""
    if not labels:
        return False
    return all(labels.get(key) == value for key, value in ownership.labels().items())
