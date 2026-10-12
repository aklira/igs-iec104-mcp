"""The retention policy of the runs (IMPLEMENTATION.md §10; task R3).

A policy names which ended runs are removed: all but the newest `keep_runs`, and each
run that ended more than `keep_days` ago. A run that has not ended is never selected.
The selection is a pure function of the records, so it can be read before anything is
removed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from igs_iec104_mcp.models.run import TERMINAL
from igs_iec104_mcp.storage.runs import RunRecord

MILLISECONDS_PER_DAY = 86_400_000


@dataclass(frozen=True)
class RetentionPolicy:
    """What is kept of the ended runs. At least one of the two limits is set."""

    keep_runs: int | None = None
    keep_days: float | None = None

    def __post_init__(self) -> None:
        if self.keep_runs is None and self.keep_days is None:
            raise ValueError("a retention policy keeps runs, by count or by age")
        if self.keep_runs is not None and self.keep_runs < 0:
            raise ValueError("keep_runs is zero or more")
        if self.keep_days is not None and self.keep_days <= 0:
            raise ValueError("keep_days is more than zero")

    def describe(self) -> str:
        parts = []
        if self.keep_runs is not None:
            parts.append(f"the newest {self.keep_runs} ended runs")
        if self.keep_days is not None:
            parts.append(f"runs that ended within {self.keep_days:g} days")
        return "keeps " + " and ".join(parts)

    def select(self, records: Sequence[RunRecord], now_ms: int) -> list[RunRecord]:
        """The ended runs the policy removes, oldest first."""
        ended = sorted(
            (r for r in records if r.status in TERMINAL and r.ended_at_ms is not None),
            key=lambda r: r.created_at_ms,
        )
        selected: set[str] = set()
        if self.keep_runs is not None:
            excess = len(ended) - self.keep_runs
            selected.update(r.id for r in ended[: max(excess, 0)])
        if self.keep_days is not None:
            cutoff = now_ms - int(self.keep_days * MILLISECONDS_PER_DAY)
            selected.update(
                r.id
                for r in ended
                if r.ended_at_ms is not None and r.ended_at_ms < cutoff
            )
        return [r for r in ended if r.id in selected]
