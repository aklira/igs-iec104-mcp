"""The business logic of the definition tools: validate, create, read, update, and list
the bench and scenario definitions of a project (IMPLEMENTATION.md §9; task M1).

The functions are plain. The MCP tools in `server.py` call them, and so do the tests. A
failure is a `ServiceError` whose code is one of `CODES`, so that a client can tell the
kinds of failure apart. A validation that finds problems is a result, not an error: the
definition is simply not valid, and the result lists each problem.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from pydantic import BaseModel, Field

from igs_iec104_mcp.definitions.store import (
    Exists,
    InvalidName,
    Missing,
    NameMismatch,
    ProjectStore,
    StaleRevision,
    Stored,
    StoreError,
)
from igs_iec104_mcp.models.common import DefinitionError, Issue
from igs_iec104_mcp.models.loader import load_bench, load_scenario
from igs_iec104_mcp.models.scenario import check_scenario

Kind = Literal["bench", "scenario"]
CODES: tuple[str, ...] = (
    "invalid_definition",
    "invalid_name",
    "not_found",
    "already_exists",
    "stale_revision",
    "invalid_argument",
    "storage_error",
    "invalid_state",
    "unsupported",
    "engine_unavailable",
)
DEFAULT_LIMIT: int = 50
MAX_LIMIT: int = 200


class ServiceError(Exception):
    """A request that failed: a stable code (see `CODES`), and a message that says what
    to change."""

    def __init__(self, code: str, message: str) -> None:
        if code not in CODES:
            raise ValueError(f"{code!r} is not an error code of the service")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class IssueOut(BaseModel):
    """A problem of a definition: where it is, and what to change."""

    path: str
    message: str


class Validation(BaseModel):
    """The result of a validation: whether the definition is valid, and its problems."""

    valid: bool
    issues: list[IssueOut] = Field(default_factory=list)


class Definition(BaseModel):
    """A definition as it is stored: its name, its revision, and its text."""

    kind: Kind
    name: str
    revision: str
    text: str


class Page(BaseModel):
    """A page of names. `next_cursor` is the name to pass to get the next page, or None
    when this page is the last."""

    items: list[str]
    next_cursor: str | None


def validate(kind: Kind, text: str, project: ProjectStore | None = None) -> Validation:
    """Validates the text of a definition. A scenario is also checked against its bench
    when the project has it."""
    if kind == "bench":
        try:
            load_bench(text)
        except DefinitionError as error:
            return _invalid(error.issues)
        return Validation(valid=True)
    try:
        scenario = load_scenario(text)
    except DefinitionError as error:
        return _invalid(error.issues)
    if project is None:
        return Validation(valid=True)
    try:
        bench = project.bench(scenario.bench)
    except Missing:
        return _invalid(
            [
                Issue(
                    "bench",
                    f"there is no bench named {scenario.bench!r} in the project: "
                    "create it first",
                )
            ]
        )
    except DefinitionError as error:
        return _invalid(
            [Issue("bench", f"the bench {scenario.bench!r} is not valid: {error}")]
        )
    except StoreError as error:
        return _invalid([Issue("bench", str(error))])
    try:
        check_scenario(scenario, bench)
    except DefinitionError as error:
        return _invalid(error.issues)
    return Validation(valid=True)


def create(store: ProjectStore, kind: Kind, name: str, text: str) -> Definition:
    """Stores a new definition. Raises already_exists when the name is taken."""
    with _errors():
        return _definition(kind, store.write(kind, name, text, expected=None))


def get(store: ProjectStore, kind: Kind, name: str) -> Definition:
    """The definition of a name, with its revision."""
    with _errors():
        return _definition(kind, store.read(kind, name))


def update(
    store: ProjectStore, kind: Kind, name: str, text: str, revision: str
) -> Definition:
    """Replaces a definition. `revision` is the one it was read at: a definition that
    changed since is not replaced (stale_revision)."""
    with _errors():
        return _definition(kind, store.write(kind, name, text, expected=revision))


def list_names(
    store: ProjectStore,
    kind: Kind,
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> Page:
    """A page of the names of a kind, in order, after the cursor."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ServiceError(
            "invalid_argument", f"the limit is {limit}: it is 1 to {MAX_LIMIT}"
        )
    names = [name for name in store.names(kind) if cursor is None or name > cursor]
    items = names[:limit]
    more = len(names) > limit
    return Page(items=items, next_cursor=items[-1] if more and items else None)


def _invalid(issues: list[Issue]) -> Validation:
    return Validation(
        valid=False,
        issues=[IssueOut(path=issue.path, message=issue.message) for issue in issues],
    )


def _definition(kind: Kind, stored: Stored) -> Definition:
    return Definition(
        kind=kind, name=stored.name, revision=stored.revision, text=stored.text
    )


@contextmanager
def _errors() -> Iterator[None]:
    """Turns the store's failures into the service's error codes."""
    try:
        yield
    except DefinitionError as error:
        raise ServiceError("invalid_definition", _describe(error.issues)) from error
    except InvalidName as error:
        raise ServiceError("invalid_name", str(error)) from error
    except NameMismatch as error:
        raise ServiceError("invalid_definition", str(error)) from error
    except Missing as error:
        raise ServiceError("not_found", str(error)) from error
    except Exists as error:
        raise ServiceError("already_exists", str(error)) from error
    except StaleRevision as error:
        raise ServiceError("stale_revision", str(error)) from error
    except StoreError as error:
        raise ServiceError("storage_error", str(error)) from error


def _describe(issues: list[Issue]) -> str:
    return "; ".join(str(issue) for issue in issues)
