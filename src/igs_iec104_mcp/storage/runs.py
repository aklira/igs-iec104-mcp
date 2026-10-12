"""The run store: the metadata of each run and its artifacts in SQLite, and its event
journal and artifact files on disk (IMPLEMENTATION.md §10; task R1).

The layout of the project directory is:

    runs.sqlite3                     metadata, statuses, and the artifact index
    runs/<run-id>/events.jsonl       the event journal, one JSON object per line
    runs/<run-id>/artifacts/<name>   the artifact files, named as the index names them

A run's status moves only along `models.run.TRANSITIONS`; an ended run moves nowhere.
The journal is bounded: once it holds `max_events` events, the next ones are counted as
dropped and not kept, and the run says how many. An artifact is written once, its
SHA-256 is recorded, and an artifact larger than `max_artifact_bytes` is refused.
Nothing here depends on a Docker resource, so a run's records survive the removal of its
test bench.

One process owns a store at a time: its calls are serialised by a lock, not by the file
system, so two controllers must not write the same directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from igs_iec104_mcp.models.common import canonical_json
from igs_iec104_mcp.models.run import RUN_ID_PATTERN, TERMINAL, TRANSITIONS, RunStatus

DATABASE = "runs.sqlite3"
RUNS_DIRECTORY = "runs"
EVIDENCE_DIRECTORY = "evidence"
JOURNAL = "events.jsonl"
ARTIFACTS = "artifacts"
SCHEMA_VERSION = 1
DEFAULT_MAX_EVENTS = 10_000
DEFAULT_MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
DEFAULT_PAGE = 50
MAX_PAGE = 200
ARTIFACT_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")

SCHEMA = """
CREATE TABLE runs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    bench TEXT NOT NULL,
    scenario TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    ended_at_ms INTEGER,
    events INTEGER NOT NULL,
    dropped INTEGER NOT NULL
);
CREATE TABLE artifacts (
    run_id TEXT NOT NULL REFERENCES runs(id),
    name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    PRIMARY KEY (run_id, name)
);
"""


class RunError(Exception):
    """A run store request that cannot be carried out. The message says why."""


class UnknownRun(RunError):
    """No run of that identity is in the store."""


class InvalidTransition(RunError):
    """A run cannot move to that status from its current one."""


class RunEnded(RunError):
    """A run has ended: it takes no more events."""


class ArtifactRefused(RunError):
    """An artifact cannot be stored: its name is invalid, taken, or too large."""


@dataclass(frozen=True)
class RunRecord:
    """A run as the store holds it: its identity, its inputs by name, its status and
    times, and how many events its journal kept and dropped."""

    id: str
    bench: str
    scenario: str
    status: RunStatus
    created_at_ms: int
    ended_at_ms: int | None
    events: int
    dropped: int


@dataclass(frozen=True)
class ArtifactRecord:
    """An artifact of a run: its name, the SHA-256 of its bytes, and its size."""

    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class RunPage:
    """A page of runs, in creation order. `next_cursor` is the cursor of the next
    page, or None on the last page."""

    items: list[RunRecord]
    next_cursor: int | None


@dataclass(frozen=True)
class EventPage:
    """A page of journal events, in order. `next_cursor` is the cursor of the next
    page, or None on the last page."""

    events: list[dict[str, Any]]
    next_cursor: int | None


class RunStore:
    """The runs of one project directory: their records, journals, and artifacts."""

    def __init__(
        self,
        root: Path,
        *,
        max_events: int = DEFAULT_MAX_EVENTS,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    ) -> None:
        if not root.is_dir():
            raise RunError(f"the project directory {str(root)!r} does not exist")
        if max_events < 1 or max_artifact_bytes < 1:
            raise ValueError("the journal and artifact limits are at least 1")
        self.root = root.resolve()
        self.max_events = max_events
        self.max_artifact_bytes = max_artifact_bytes
        self._lock = threading.RLock()
        (self.root / RUNS_DIRECTORY).mkdir(exist_ok=True)
        self._db = sqlite3.connect(
            self.root / DATABASE, check_same_thread=False, isolation_level=None
        )
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def create(
        self, run_id: str, bench: str, scenario: str, created_at_ms: int
    ) -> RunRecord:
        """A new run, pending. Its directory and its empty journal are made now."""
        _check_run_id(run_id)
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO runs (id, bench, scenario, status, created_at_ms, "
                    "ended_at_ms, events, dropped) "
                    "VALUES (?, ?, ?, ?, ?, NULL, 0, 0)",
                    (run_id, bench, scenario, RunStatus.PENDING.value, created_at_ms),
                )
            except sqlite3.IntegrityError as error:
                raise RunError(f"a run {run_id!r} already exists") from error
            directory = self._contained(self.root / RUNS_DIRECTORY / run_id)
            (directory / ARTIFACTS).mkdir(parents=True, exist_ok=True)
            self._contained(directory / JOURNAL).touch()
        return self.get(run_id)

    def get(self, run_id: str) -> RunRecord:
        """The record of a run. Raises UnknownRun when there is none."""
        row = None
        with self._lock:
            if re.fullmatch(RUN_ID_PATTERN, run_id):
                row = self._db.execute(
                    "SELECT id, bench, scenario, status, created_at_ms, "
                    "ended_at_ms, events, dropped FROM runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
        if row is None:
            raise UnknownRun(f"there is no run {run_id!r} in the store")
        return _record(row)

    def move(
        self, run_id: str, target: RunStatus, *, ended_at_ms: int | None = None
    ) -> RunRecord:
        """Moves a run to a status. An ended status needs the time it ended; the other
        statuses take none. Raises InvalidTransition for a move the model refuses."""
        with self._lock:
            current = self.get(run_id)
            if target not in TRANSITIONS.get(current.status, frozenset()):
                raise InvalidTransition(
                    f"a run that is {current.status} cannot be {target}"
                )
            ended = target in TERMINAL
            if ended and ended_at_ms is None:
                raise RunError(f"a run that is {target} needs the time it ended")
            if not ended and ended_at_ms is not None:
                raise RunError(f"a run that is {target} has not ended")
            if ended_at_ms is not None and ended_at_ms < current.created_at_ms:
                raise RunError("a run cannot end before it was created")
            self._db.execute(
                "UPDATE runs SET status = ?, ended_at_ms = ? WHERE id = ?",
                (target.value, ended_at_ms, run_id),
            )
        return self.get(run_id)

    def record(self, run_id: str, kind: str, fields: dict[str, Any]) -> int | None:
        """Appends an event to the run's journal, and returns its sequence number.

        When the journal is full, the event is counted as dropped and None is returned.
        A journal line is `seq`, `kind`, and the fields, in canonical JSON. A run that
        has ended takes no more events (RunEnded).
        """
        if "seq" in fields or "kind" in fields:
            raise ValueError("'seq' and 'kind' are the store's: do not pass them")
        with self._lock:
            run = self.get(run_id)
            if run.status in TERMINAL:
                raise RunEnded(f"the run {run_id!r} has ended: no more events")
            if run.events >= self.max_events:
                self._db.execute(
                    "UPDATE runs SET dropped = dropped + 1 WHERE id = ?", (run_id,)
                )
                return None
            sequence = run.events + 1
            line = canonical_json({"seq": sequence, "kind": kind, **fields}) + "\n"
            journal = self._contained(self._journal(run_id))
            with journal.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            self._db.execute(
                "UPDATE runs SET events = ? WHERE id = ?", (sequence, run_id)
            )
            return sequence

    def events(
        self, run_id: str, *, after: int = 0, limit: int = DEFAULT_PAGE
    ) -> EventPage:
        """The journal events after the sequence number `after`, a page at a time.

        The journal is read from its start. It is bounded, so the read is linear in its
        size, and a later version may index it.
        """
        _check_limit(limit)
        with self._lock:
            self.get(run_id)
            selected: list[dict[str, Any]] = []
            journal = self._contained(self._journal(run_id))
            with journal.open(encoding="utf-8") as handle:
                for line in handle:
                    event = json.loads(line)
                    if event["seq"] > after:
                        selected.append(event)
                        if len(selected) > limit:
                            break
        more = len(selected) > limit
        page = selected[:limit]
        return EventPage(
            events=page,
            next_cursor=page[-1]["seq"] if more and page else None,
        )

    def add_artifact(self, run_id: str, name: str, data: bytes) -> ArtifactRecord:
        """Stores an artifact of a run, once.

        Its name is a plain file name. It is refused when the name is not valid, when
        the name is taken, or when the data is larger than the limit.
        """
        if not ARTIFACT_NAME.fullmatch(name):
            raise ArtifactRefused(
                f"{name!r} is not a valid artifact name: lower-case letters, digits, "
                "dots, hyphens, and underscores, starting with a letter or a digit"
            )
        if len(data) > self.max_artifact_bytes:
            raise ArtifactRefused(
                f"the artifact is {len(data)} bytes, and the limit is "
                f"{self.max_artifact_bytes}"
            )
        with self._lock:
            self.get(run_id)
            path = self._contained(self._artifact(run_id, name))
            if path.exists():
                raise ArtifactRefused(
                    f"an artifact named {name!r} already exists: an artifact is "
                    "written once"
                )
            _write_new(path, data)
            digest = hashlib.sha256(data).hexdigest()
            self._db.execute(
                "INSERT INTO artifacts (run_id, name, sha256, size) "
                "VALUES (?, ?, ?, ?)",
                (run_id, name, digest, len(data)),
            )
        return ArtifactRecord(name=name, sha256=digest, size=len(data))

    def artifacts(self, run_id: str) -> list[ArtifactRecord]:
        """The artifacts of a run, by name."""
        with self._lock:
            self.get(run_id)
            rows = self._db.execute(
                "SELECT name, sha256, size FROM artifacts WHERE run_id = ? "
                "ORDER BY name",
                (run_id,),
            ).fetchall()
        return [ArtifactRecord(name=n, sha256=s, size=z) for n, s, z in rows]

    def read_artifact(self, run_id: str, name: str) -> bytes:
        """The bytes of an artifact, as they were stored."""
        with self._lock:
            self.get(run_id)
            row = self._db.execute(
                "SELECT 1 FROM artifacts WHERE run_id = ? AND name = ?", (run_id, name)
            ).fetchone()
            if row is None:
                raise UnknownRun(f"run {run_id!r} has no artifact named {name!r}")
            return self._contained(self._artifact(run_id, name)).read_bytes()

    def list_runs(self, *, limit: int = DEFAULT_PAGE, cursor: int = 0) -> RunPage:
        """A page of runs in creation order, after the cursor.

        A cursor is the sequence number of the last run of the previous page. Zero
        starts at the first run.
        """
        _check_limit(limit)
        with self._lock:
            rows = self._db.execute(
                "SELECT seq, id, bench, scenario, status, created_at_ms, "
                "ended_at_ms, events, dropped FROM runs WHERE seq > ? "
                "ORDER BY seq LIMIT ?",
                (cursor, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        page = rows[:limit]
        return RunPage(
            items=[_record(row[1:]) for row in page],
            next_cursor=page[-1][0] if more and page else None,
        )

    def delete(self, run_id: str) -> None:
        """Removes an ended run: its record, its artifact index, its journal, its
        artifact files, and its evidence. A run that has not ended is refused."""
        with self._lock:
            run = self.get(run_id)
            if run.status not in TERMINAL:
                raise RunError(
                    f"the run {run_id!r} is {run.status}: only an ended run is removed"
                )
            self._db.execute("DELETE FROM artifacts WHERE run_id = ?", (run_id,))
            self._db.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            shutil.rmtree(self._contained(self.root / RUNS_DIRECTORY / run_id))

    def unended(self) -> list[RunRecord]:
        """Every run that has not ended, pending or running, in creation order."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, bench, scenario, status, created_at_ms, ended_at_ms, "
                "events, dropped FROM runs WHERE status IN (?, ?) ORDER BY seq",
                (RunStatus.PENDING.value, RunStatus.RUNNING.value),
            ).fetchall()
        return [_record(row) for row in rows]

    def _migrate(self) -> None:
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RunError(
                f"the run store is version {version}, and this server reads version "
                f"{SCHEMA_VERSION}: use a newer server"
            )
        if version == 0:
            self._db.executescript(SCHEMA)
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _journal(self, run_id: str) -> Path:
        return self.root / RUNS_DIRECTORY / run_id / JOURNAL

    def _artifact(self, run_id: str, name: str) -> Path:
        return self.root / RUNS_DIRECTORY / run_id / ARTIFACTS / name

    def _contained(self, path: Path) -> Path:
        """The path, once it is checked to stay in the project directory."""
        if not path.resolve().is_relative_to(self.root):
            raise RunError(f"{path.name!r} leads outside the project directory")
        return path


def _check_run_id(run_id: str) -> None:
    if not re.fullmatch(RUN_ID_PATTERN, run_id):
        raise RunError(f"{run_id!r} is not a run identity: it has 32 hex digits")


def _check_limit(limit: int) -> None:
    if not 1 <= limit <= MAX_PAGE:
        raise RunError(f"the page size is {limit}: it is 1 to {MAX_PAGE}")


def _record(row: tuple[Any, ...]) -> RunRecord:
    run_id, bench, scenario, status, created, ended, events, dropped = row
    return RunRecord(
        id=run_id,
        bench=bench,
        scenario=scenario,
        status=RunStatus(status),
        created_at_ms=created,
        ended_at_ms=ended,
        events=events,
        dropped=dropped,
    )


def _write_new(path: Path, data: bytes) -> None:
    """Writes a file in one step: a temporary file in the same directory is flushed,
    and then renamed over the path."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
