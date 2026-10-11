"""A run: one execution of a scenario, with the snapshot of its inputs, its status, its
progress, and its evidence (IMPLEMENTATION.md §5, §10).

The snapshot holds the bench and the scenario as they were when the run started, with
their SHA-256 hashes, so that an edit of the files afterwards does not change what the
run did. The status and the timestamps must agree: a run that has ended has an end time,
and one that has not, does not."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from igs_iec104_mcp.models.bench import TestBench
from igs_iec104_mcp.models.common import Definition, SchemaVersion, sha256_of
from igs_iec104_mcp.models.scenario import Scenario

HEX64_PATTERN = r"^[0-9a-f]{64}$"
RUN_ID_PATTERN = r"^[0-9a-f]{32}$"


class RunStatus(StrEnum):
    """Where a run is. The last five end it."""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    ERROR = "error"


TERMINAL: frozenset[RunStatus] = frozenset(
    {
        RunStatus.PASSED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
        RunStatus.ERROR,
    }
)


class Outcome(StrEnum):
    """The verdict of one step (§7): confirmed, refused, timed out, cancelled, and "
    "the others."""

    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class Progress(Definition):
    completed: int = Field(ge=0)
    total: int = Field(ge=0)

    @model_validator(mode="after")
    def _check(self) -> Progress:
        if self.completed > self.total:
            raise ValueError(
                f"progress {self.completed} of {self.total} steps is more than the "
                f"total"
            )
        return self


class Snapshot(Definition):
    """The inputs of a run, as they were at its start, with their hashes."""

    bench: dict[str, Any]
    bench_sha256: str = Field(pattern=HEX64_PATTERN)
    scenario: dict[str, Any]
    scenario_sha256: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _check_hashes(self) -> Snapshot:
        if sha256_of(self.bench) != self.bench_sha256:
            raise ValueError(
                "the bench's SHA-256 does not match the bench in the snapshot: the "
                "snapshot was changed"
            )
        if sha256_of(self.scenario) != self.scenario_sha256:
            raise ValueError(
                "the scenario's SHA-256 does not match the scenario in the snapshot: "
                "the snapshot was changed"
            )
        TestBench.model_validate(self.bench)
        Scenario.model_validate(self.scenario)
        return self


class Observation(Definition):
    step: str
    passed: bool
    detail: str
    observed_at_ms: int = Field(ge=0)


class Verdict(Definition):
    step: str
    outcome: Outcome
    detail: str


class Artifact(Definition):
    name: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=HEX64_PATTERN)


class Run(Definition):
    """The record of one execution of a scenario."""

    schema_version: SchemaVersion
    id: str = Field(
        pattern=RUN_ID_PATTERN,
        description="A random identity of 32 lower-case hexadecimal digits.",
    )
    status: RunStatus
    started_at_ms: int = Field(ge=0)
    ended_at_ms: int | None = Field(default=None, ge=0)
    snapshot: Snapshot
    progress: Progress
    observations: list[Observation] = Field(default_factory=list)
    verdicts: list[Verdict] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_status(self) -> Run:
        ended = self.status in TERMINAL
        if ended and self.ended_at_ms is None:
            raise ValueError(
                f"a run that is {self.status} has ended, so it needs 'ended_at_ms'"
            )
        if not ended and self.ended_at_ms is not None:
            raise ValueError(
                f"a run that is {self.status} has not ended, so 'ended_at_ms' must be "
                f"absent"
            )
        if (
            ended
            and self.ended_at_ms is not None
            and self.ended_at_ms < self.started_at_ms
        ):
            raise ValueError("'ended_at_ms' is before 'started_at_ms'")
        if self.status is RunStatus.PASSED and any(
            v.outcome is not Outcome.PASSED for v in self.verdicts
        ):
            raise ValueError(
                "a passed run has a step that did not pass: its status must be failed"
            )
        return self


def snapshot_of(bench: TestBench, scenario: Scenario) -> Snapshot:
    """The snapshot of the inputs of a run, with their hashes."""
    bench_data = bench.model_dump(mode="json")
    scenario_data = scenario.model_dump(mode="json")
    return Snapshot(
        bench=bench_data,
        bench_sha256=sha256_of(bench_data),
        scenario=scenario_data,
        scenario_sha256=sha256_of(scenario_data),
    )
