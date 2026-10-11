"""The project directory of the definitions: where the bench and scenario files live,
how they are written, and the snapshots of the runs (IMPLEMENTATION.md §5).

The layout of the project directory is:

    benches/<name>.yaml
    scenarios/<name>.yaml
    runs/<run-id>/snapshot.json

A name is checked before it becomes a file name, and a path is resolved before it is
used: a path that leads outside the project directory, by a `..`, or by a symbolic link,
is refused. A write goes to a temporary file in the same directory, is flushed to disk,
and then replaces the file in one step, so a reader sees the old text or the new text,
never a part of one. Every update names the revision it was read at (the SHA-256 of the
file's bytes), and it is refused when the file has changed since. Writers hold an
exclusive lock on the project directory, so that the check and the replacement are one
step, also between processes. A run's snapshot is written once and never changed."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from igs_iec104_mcp.models.bench import TestBench
from igs_iec104_mcp.models.common import check_name
from igs_iec104_mcp.models.loader import load_bench, load_scenario
from igs_iec104_mcp.models.run import RUN_ID_PATTERN, Snapshot, snapshot_of
from igs_iec104_mcp.models.scenario import Scenario, check_scenario

Kind = Literal["bench", "scenario"]
DIRECTORIES: dict[Kind, str] = {"bench": "benches", "scenario": "scenarios"}
READERS: dict[Kind, Callable[[str], BaseModel]] = {
    "bench": load_bench,
    "scenario": load_scenario,
}
LOCK_FILE = ".lock"
RUNS_DIRECTORY = "runs"
SNAPSHOT_FILE = "snapshot.json"


class StoreError(Exception):
    """A definition could not be read or stored. The message says why and what to do."""


class StaleRevision(StoreError):
    """The file changed, or was removed, since it was read: read it again, merge the
    change, and write again."""


class Missing(StoreError):
    """No definition of that name is in the project."""


class Exists(StoreError):
    """A definition of that name is already in the project."""


@dataclass(frozen=True)
class Stored:
    """A definition file as it is on disk: its name, its text, and its revision."""

    name: str
    text: str
    revision: str


def revision_of(data: bytes) -> str:
    """The revision of a file: the SHA-256 of its bytes."""
    return hashlib.sha256(data).hexdigest()


def new_run_id() -> str:
    """A random identity of a run: 32 lower-case hexadecimal digits."""
    return secrets.token_hex(16)


class ProjectStore:
    """The definitions of one project directory, read and written with their
    revisions."""

    def __init__(self, root: Path) -> None:
        if not root.is_dir():
            raise StoreError(
                f"the project directory {str(root)!r} does not exist: create it, or "
                "choose another"
            )
        self.root = root.resolve()

    def names(self, kind: Kind) -> list[str]:
        """The names of the definitions of a kind, in order."""
        directory = self.root / DIRECTORIES[kind]
        if not directory.is_dir():
            return []
        return sorted(
            path.stem
            for path in directory.glob("*.yaml")
            if _is_name(path.stem) and self._is_inside(path)
        )

    def read(self, kind: Kind, name: str) -> Stored:
        """The file of a definition, with its revision. Raises Missing when it is not
        there."""
        path = self._file(kind, name)
        try:
            data = path.read_bytes()
        except FileNotFoundError as error:
            raise Missing(
                f"there is no {kind} named {name!r} in the project: create it, or "
                "check its name"
            ) from error
        return _stored(name, data)

    def bench(self, name: str) -> TestBench:
        """The bench of a name, read and checked."""
        return _checked(load_bench(self.read("bench", name).text), name)

    def scenario(self, name: str) -> Scenario:
        """The scenario of a name, read and checked."""
        return _checked(load_scenario(self.read("scenario", name).text), name)

    def write(
        self, kind: Kind, name: str, text: str, *, expected: str | None
    ) -> Stored:
        """
        Stores the text of a definition. `expected` is the revision the definition was
        read at, or None for a definition that must not exist yet. The text is checked
        first, and an invalid one is not written. Raises StaleRevision when the file
        changed since it was read, and Exists when a new definition's name is taken.
        """
        path = self._file(kind, name)
        _checked(READERS[kind](text), name)
        data = text.encode("utf-8")
        with self._locked():
            current = _revision_on_disk(path)
            if expected is None and current is not None:
                raise Exists(
                    f"a {kind} named {name!r} already exists: read it, and update it "
                    "with its revision"
                )
            if expected is not None and current != expected:
                raise StaleRevision(_stale_message(kind, name, current))
            _replace(path, data)
        return _stored(name, data)

    def snapshot_run(
        self, run_id: str, bench_name: str, scenario_name: str
    ) -> Snapshot:
        """
        Takes the snapshot of a run's inputs: the bench and the scenario as they are
        now, checked against each other, and written to `runs/<run-id>/snapshot.json`.
        A later edit of either file does not change the snapshot.
        """
        path = self._run_file(run_id)
        bench = self.bench(bench_name)
        scenario = self.scenario(scenario_name)
        check_scenario(scenario, bench)
        snapshot = snapshot_of(bench, scenario)
        text = json.dumps(snapshot.model_dump(mode="json"), indent=2, sort_keys=True)
        with self._locked():
            if path.exists():
                raise Exists(
                    f"run {run_id} already has a snapshot: it is never changed"
                )
            _replace(path, (text + "\n").encode("utf-8"))
        return snapshot

    def read_snapshot(self, run_id: str) -> Snapshot:
        """The snapshot of a run. Raises StoreError when it is missing or was changed,
        since its hashes are checked when it is read."""
        path = self._run_file(run_id)
        try:
            return Snapshot.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise Missing(f"run {run_id} has no snapshot") from error
        except ValidationError as error:
            raise StoreError(
                f"the snapshot of run {run_id} does not match its hashes: it was "
                "changed"
            ) from error

    def _file(self, kind: Kind, name: str) -> Path:
        if kind not in DIRECTORIES:
            raise StoreError(
                f"{kind!r} is not a kind of definition: use 'bench' or 'scenario'"
            )
        if not _is_name(name):
            raise StoreError(
                f"{name!r} is not a valid name: use lower-case letters, digits, and "
                "hyphens, starting with a letter, and no path"
            )
        path = self.root / DIRECTORIES[kind] / f"{name}.yaml"
        if not self._is_inside(path):
            raise StoreError(f"{name!r} leads outside the project directory: refused")
        return path

    def _run_file(self, run_id: str) -> Path:
        if not re.fullmatch(RUN_ID_PATTERN, run_id):
            raise StoreError(f"{run_id!r} is not a run identity: it has 32 hex digits")
        path = self.root / RUNS_DIRECTORY / run_id / SNAPSHOT_FILE
        if not self._is_inside(path):
            raise StoreError(f"run {run_id!r} leads outside the project directory")
        return path

    def _is_inside(self, path: Path) -> bool:
        """Whether a path, with every symbolic link in it followed, stays in the project
        directory."""
        return path.resolve().is_relative_to(self.root)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        # O_NOFOLLOW: a lock file that is a symbolic link is refused, not followed.
        descriptor = os.open(
            self.root / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644
        )
        with os.fdopen(descriptor, "r+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _is_name(value: str) -> bool:
    try:
        check_name(value)
    except ValueError:
        return False
    return True


def _checked[M: BaseModel](model: M, name: str) -> M:
    """The model, when its own name is the name of its file. Raises StoreError
    otherwise, so that a file is found by the name it has."""
    own = getattr(model, "name", None)
    if isinstance(model, TestBench | Scenario) and own != name:
        raise StoreError(
            f"the file is named {name!r}, but the definition says name: {own!r}: "
            "rename one of them"
        )
    return model


def _stored(name: str, data: bytes) -> Stored:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise StoreError(f"{name!r} is not UTF-8 text") from error
    return Stored(name=name, text=text, revision=revision_of(data))


def _revision_on_disk(path: Path) -> str | None:
    try:
        return revision_of(path.read_bytes())
    except FileNotFoundError:
        return None


def _stale_message(kind: str, name: str, current: str | None) -> str:
    if current is None:
        return f"the {kind} {name!r} was removed since it was read: read it again"
    return (
        f"the {kind} {name!r} changed since it was read: read it again, merge the "
        "change, and write it with the new revision"
    )


def _replace(path: Path, data: bytes) -> None:
    """Replaces the file with the data in one step: the data is flushed to a temporary
    file in the same directory first, and the temporary file is removed when the write
    fails."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    _sync_directory(path.parent)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
