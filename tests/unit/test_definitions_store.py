"""The project directory: names and paths are contained, writes are atomic and checked
against the revision they were read at, and a run's snapshot is unaffected by later
edits (IMPLEMENTATION.md §5)."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from igs_iec104_mcp.definitions.store import (
    Exists,
    Missing,
    ProjectStore,
    StaleRevision,
    StoreError,
    new_run_id,
)
from igs_iec104_mcp.models.common import DefinitionError

ROOT = Path(__file__).resolve().parents[2]
BENCH_TEXT = (ROOT / "examples/benches/basic-pair.yaml").read_text(encoding="utf-8")
SCENARIO_NAME = "interrogation-and-spontaneous-update"
SCENARIO_TEXT = (ROOT / "examples/scenarios" / f"{SCENARIO_NAME}.yaml").read_text(
    encoding="utf-8"
)


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    project = tmp_path / "project"
    project.mkdir()
    return ProjectStore(project)


def test_the_project_directory_must_exist(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="does not exist"):
        ProjectStore(tmp_path / "missing")


def test_a_definition_is_written_and_read_back_with_its_revision(
    store: ProjectStore,
) -> None:
    created = store.write("bench", "basic-pair", BENCH_TEXT, expected=None)
    read = store.read("bench", "basic-pair")
    assert read == created
    assert read.text == BENCH_TEXT
    assert store.bench("basic-pair").name == "basic-pair"
    assert store.names("bench") == ["basic-pair"]
    assert store.names("scenario") == []


def test_a_missing_definition_is_reported_by_its_name(store: ProjectStore) -> None:
    with pytest.raises(Missing, match="no bench named 'basic-pair'"):
        store.read("bench", "basic-pair")


@pytest.mark.parametrize(
    "name",
    ["../escape", "a/b", "/etc/passwd", "..", ".hidden", "Upper", "", "x" * 64],
)
def test_a_name_that_is_not_a_plain_name_is_refused(
    store: ProjectStore, name: str
) -> None:
    with pytest.raises(StoreError, match="not a valid name"):
        store.read("bench", name)
    with pytest.raises(StoreError, match="not a valid name"):
        store.write("bench", name, BENCH_TEXT, expected=None)
    assert store.names("bench") == []


def test_an_unknown_kind_is_refused(store: ProjectStore) -> None:
    with pytest.raises(StoreError, match="not a kind of definition"):
        store.read("run", "basic-pair")  # type: ignore[arg-type]


def test_a_symbolic_link_to_a_file_outside_is_neither_read_nor_written(
    store: ProjectStore, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text(BENCH_TEXT.replace("name: basic-pair", "name: leak"), "utf-8")
    (store.root / "benches").mkdir()
    os.symlink(outside, store.root / "benches" / "leak.yaml")

    with pytest.raises(StoreError, match="outside the project directory"):
        store.read("bench", "leak")
    edited = BENCH_TEXT.replace("name: basic-pair", "name: leak")
    with pytest.raises(StoreError, match="outside the project directory"):
        store.write("bench", "leak", edited, expected=None)
    assert outside.read_text(encoding="utf-8") == edited
    assert store.names("bench") == []


def test_a_directory_linked_outside_the_project_is_refused(
    store: ProjectStore, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, store.root / "benches")

    with pytest.raises(StoreError, match="outside the project directory"):
        store.write("bench", "basic-pair", BENCH_TEXT, expected=None)
    assert list(elsewhere.iterdir()) == []


def test_a_lock_file_that_is_a_link_is_not_followed(
    store: ProjectStore, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    os.symlink(outside, store.root / ".lock")

    with pytest.raises(OSError):
        store.write("bench", "basic-pair", BENCH_TEXT, expected=None)
    assert outside.read_text(encoding="utf-8") == "keep\n"


def test_an_update_with_a_stale_revision_is_refused(store: ProjectStore) -> None:
    created = store.write("bench", "basic-pair", BENCH_TEXT, expected=None)
    first = BENCH_TEXT.replace("value: 12.5", "value: 13.5")
    updated = store.write("bench", "basic-pair", first, expected=created.revision)
    assert updated.revision != created.revision

    second = BENCH_TEXT.replace("value: 12.5", "value: 14.5")
    with pytest.raises(StaleRevision, match="changed since it was read"):
        store.write("bench", "basic-pair", second, expected=created.revision)
    assert store.read("bench", "basic-pair").text == first


def test_an_update_of_a_definition_removed_since_it_was_read_is_refused(
    store: ProjectStore,
) -> None:
    created = store.write("bench", "basic-pair", BENCH_TEXT, expected=None)
    (store.root / "benches" / "basic-pair.yaml").unlink()
    with pytest.raises(StaleRevision, match="was removed since it was read"):
        store.write("bench", "basic-pair", BENCH_TEXT, expected=created.revision)


def test_a_new_definition_cannot_replace_an_existing_one(store: ProjectStore) -> None:
    store.write("bench", "basic-pair", BENCH_TEXT, expected=None)
    with pytest.raises(Exists, match="already exists"):
        store.write("bench", "basic-pair", BENCH_TEXT, expected=None)


def test_concurrent_updates_at_one_revision_let_only_one_through(
    store: ProjectStore,
) -> None:
    created = store.write("bench", "basic-pair", BENCH_TEXT, expected=None)
    barrier = threading.Barrier(8)

    def update(value: int) -> str:
        text = BENCH_TEXT.replace("value: 12.5", f"value: {value}.5")
        barrier.wait()
        try:
            store.write("bench", "basic-pair", text, expected=created.revision)
        except StaleRevision:
            return "stale"
        return "written"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(update, range(1, 9)))
    assert sorted(outcomes) == ["stale"] * 7 + ["written"]


def test_an_invalid_definition_is_not_written(store: ProjectStore) -> None:
    text = BENCH_TEXT.replace("schema_version: 1", "schema_version: 2")
    with pytest.raises(DefinitionError, match="schema_version"):
        store.write("bench", "basic-pair", text, expected=None)
    with pytest.raises(Missing):
        store.read("bench", "basic-pair")


def test_the_file_name_must_be_the_name_of_the_definition(store: ProjectStore) -> None:
    with pytest.raises(StoreError, match="the file is named 'other'"):
        store.write("bench", "other", BENCH_TEXT, expected=None)


def test_a_failed_write_keeps_the_old_text_and_leaves_no_temporary_file(
    store: ProjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = store.write("bench", "basic-pair", BENCH_TEXT, expected=None)
    edited = BENCH_TEXT.replace("value: 12.5", "value: 13.5")

    def disk_full(source: str, target: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", disk_full)
    with pytest.raises(OSError, match="disk full"):
        store.write("bench", "basic-pair", edited, expected=created.revision)
    monkeypatch.undo()

    bench_directory = store.root / "benches"
    assert store.read("bench", "basic-pair").text == BENCH_TEXT
    assert [path.name for path in bench_directory.iterdir()] == ["basic-pair.yaml"]


def test_a_snapshot_is_unaffected_by_a_later_edit(store: ProjectStore) -> None:
    store.write("bench", "basic-pair", BENCH_TEXT, expected=None)
    scenario = store.write("scenario", SCENARIO_NAME, SCENARIO_TEXT, expected=None)
    run_id = new_run_id()
    taken = store.snapshot_run(run_id, "basic-pair", SCENARIO_NAME)

    edited = SCENARIO_TEXT.replace("timeout: 2s", "timeout: 3s")
    store.write("scenario", SCENARIO_NAME, edited, expected=scenario.revision)

    read = store.read_snapshot(run_id)
    assert read == taken
    assert read.scenario["steps"][-1]["timeout"] == "2s"
    assert store.read("scenario", SCENARIO_NAME).text == edited


def test_a_run_snapshot_is_written_once(store: ProjectStore) -> None:
    store.write("bench", "basic-pair", BENCH_TEXT, expected=None)
    store.write("scenario", SCENARIO_NAME, SCENARIO_TEXT, expected=None)
    run_id = new_run_id()
    store.snapshot_run(run_id, "basic-pair", SCENARIO_NAME)
    with pytest.raises(Exists, match="never changed"):
        store.snapshot_run(run_id, "basic-pair", SCENARIO_NAME)


def test_a_snapshot_changed_on_disk_is_refused(store: ProjectStore) -> None:
    store.write("bench", "basic-pair", BENCH_TEXT, expected=None)
    store.write("scenario", SCENARIO_NAME, SCENARIO_TEXT, expected=None)
    run_id = new_run_id()
    store.snapshot_run(run_id, "basic-pair", SCENARIO_NAME)

    path = store.root / "runs" / run_id / "snapshot.json"
    path.write_text(
        path.read_text(encoding="utf-8").replace("12.5", "99.5"), encoding="utf-8"
    )
    with pytest.raises(StoreError, match="does not match its hashes"):
        store.read_snapshot(run_id)


def test_a_snapshot_needs_its_bench_and_scenario(store: ProjectStore) -> None:
    store.write("scenario", SCENARIO_NAME, SCENARIO_TEXT, expected=None)
    with pytest.raises(Missing, match="no bench named 'basic-pair'"):
        store.snapshot_run(new_run_id(), "basic-pair", SCENARIO_NAME)


@pytest.mark.parametrize("run_id", ["../escape", "ABC", "0" * 31, "g" * 32])
def test_a_run_identity_that_is_not_an_identity_is_refused(
    store: ProjectStore, run_id: str
) -> None:
    store.write("bench", "basic-pair", BENCH_TEXT, expected=None)
    store.write("scenario", SCENARIO_NAME, SCENARIO_TEXT, expected=None)
    with pytest.raises(StoreError, match="not a run identity"):
        store.snapshot_run(run_id, "basic-pair", SCENARIO_NAME)
    with pytest.raises(StoreError, match="not a run identity"):
        store.read_snapshot(run_id)


def test_names_lists_the_definitions_in_order(store: ProjectStore) -> None:
    for name in ["zeta", "alpha"]:
        store.write(
            "bench", name, BENCH_TEXT.replace("basic-pair", name), expected=None
        )
    assert store.names("bench") == ["alpha", "zeta"]
