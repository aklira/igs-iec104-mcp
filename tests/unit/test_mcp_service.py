"""The business logic of the definition tools (IMPLEMENTATION.md §9; task M1).

Validation of a bench and of a scenario (against its bench when the project has it),
creation, reading, revisions, pagination, and the stable error codes of the service.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from igs_iec104_mcp.definitions import service
from igs_iec104_mcp.definitions.store import ProjectStore, revision_of

ROOT = Path(__file__).resolve().parents[2]
SCENARIO_NAME = "interrogation-and-spontaneous-update"
BENCH = (ROOT / "examples/benches/basic-pair.yaml").read_text(encoding="utf-8")
SCENARIO = (
    ROOT / "examples/scenarios/interrogation-and-spontaneous-update.yaml"
).read_text(encoding="utf-8")


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(tmp_path)


def code_of(request: Callable[[], Any]) -> str:
    with pytest.raises(service.ServiceError) as error:
        request()
    return error.value.code


def test_a_valid_bench_validates() -> None:
    assert service.validate("bench", BENCH) == service.Validation(valid=True)


def test_an_invalid_bench_lists_each_problem_with_its_path() -> None:
    result = service.validate("bench", "colour: red\n" + BENCH)
    assert result.valid is False
    assert [issue.path for issue in result.issues] == ["colour"]
    assert "is not a field of this definition" in result.issues[0].message


def test_a_document_that_is_not_yaml_is_invalid_at_the_top() -> None:
    result = service.validate("bench", "a: [\n")
    assert result.valid is False
    assert result.issues[0].path == ""
    assert "not valid YAML" in result.issues[0].message


def test_a_scenario_without_its_bench_in_the_project_says_so(
    store: ProjectStore,
) -> None:
    result = service.validate("scenario", SCENARIO, store)
    assert result.valid is False
    assert result.issues[0].path == "bench"
    assert "create it first" in result.issues[0].message


def test_a_scenario_is_checked_against_its_bench(store: ProjectStore) -> None:
    service.create(store, "bench", "basic-pair", BENCH)
    assert service.validate("scenario", SCENARIO, store) == service.Validation(
        valid=True
    )
    unknown = SCENARIO.replace("client-1", "client-9")
    result = service.validate("scenario", unknown, store)
    assert result.valid is False
    assert any("client-9" in issue.message for issue in result.issues)


def test_a_scenario_validates_alone_without_a_project() -> None:
    assert service.validate("scenario", SCENARIO) == service.Validation(valid=True)


def test_a_definition_is_created_read_back_with_its_revision(
    store: ProjectStore,
) -> None:
    created = service.create(store, "bench", "basic-pair", BENCH)
    assert created.kind == "bench"
    assert created.revision == revision_of(BENCH.encode("utf-8"))
    assert service.get(store, "bench", "basic-pair") == created


def test_a_name_that_is_taken_is_already_exists(store: ProjectStore) -> None:
    service.create(store, "bench", "basic-pair", BENCH)
    assert (
        code_of(lambda: service.create(store, "bench", "basic-pair", BENCH))
        == "already_exists"
    )


def test_a_missing_definition_is_not_found(store: ProjectStore) -> None:
    assert code_of(lambda: service.get(store, "scenario", "nothing")) == "not_found"


@pytest.mark.parametrize("name", ["Basic Pair", "../escape"])
def test_a_bad_name_is_invalid_name(store: ProjectStore, name: str) -> None:
    assert (
        code_of(lambda: service.create(store, "bench", name, BENCH)) == "invalid_name"
    )


def test_a_definition_that_does_not_validate_is_invalid_definition(
    store: ProjectStore,
) -> None:
    assert (
        code_of(lambda: service.create(store, "bench", "basic-pair", "a: [\n"))
        == "invalid_definition"
    )
    # Its own name must be the name of its file.
    assert (
        code_of(lambda: service.create(store, "bench", "other-name", BENCH))
        == "invalid_definition"
    )


def test_an_update_names_the_revision_it_was_read_at(store: ProjectStore) -> None:
    created = service.create(store, "scenario", SCENARIO_NAME, SCENARIO)
    updated_text = SCENARIO.replace("tolerance: 0.001", "tolerance: 0.002", 1)
    updated = service.update(
        store, "scenario", SCENARIO_NAME, updated_text, created.revision
    )
    assert updated.revision != created.revision
    assert service.get(store, "scenario", SCENARIO_NAME).text == updated_text
    assert (
        code_of(
            lambda: service.update(
                store, "scenario", SCENARIO_NAME, updated_text, created.revision
            )
        )
        == "stale_revision"
    )


def bench_named(name: str) -> str:
    return BENCH.replace("name: basic-pair", f"name: {name}", 1)


def test_the_names_are_listed_in_pages_with_a_cursor(store: ProjectStore) -> None:
    for name in ["a-bench", "b-bench", "c-bench"]:
        service.create(store, "bench", name, bench_named(name))
    first = service.list_names(store, "bench", limit=2)
    assert first == service.Page(items=["a-bench", "b-bench"], next_cursor="b-bench")
    second = service.list_names(store, "bench", limit=2, cursor=first.next_cursor)
    assert second == service.Page(items=["c-bench"], next_cursor=None)


def test_a_page_of_an_empty_project_is_empty(store: ProjectStore) -> None:
    assert service.list_names(store, "scenario") == service.Page(
        items=[], next_cursor=None
    )


@pytest.mark.parametrize("limit", [0, service.MAX_LIMIT + 1])
def test_a_limit_out_of_range_is_invalid_argument(
    store: ProjectStore, limit: int
) -> None:
    assert (
        code_of(lambda: service.list_names(store, "bench", limit=limit))
        == "invalid_argument"
    )


def test_a_project_directory_that_does_not_exist_stops_the_server_with_status_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from igs_iec104_mcp.server import main

    assert main(["--project", str(tmp_path / "missing")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "does not exist" in captured.err
