"""The run store (IMPLEMENTATION.md §10; task R1).

A run's status moves along the model's transitions, its journal is ordered and bounded,
its artifacts are written once and checked by size and name, its runs are paged, and its
records survive a reopen.
"""

import hashlib
import json
from pathlib import Path

import pytest

from igs_iec104_mcp.models.run import RunStatus
from igs_iec104_mcp.storage.runs import (
    ArtifactRefused,
    InvalidTransition,
    RunEnded,
    RunError,
    RunStore,
    UnknownRun,
)

RUN = "0123456789abcdef0123456789abcdef"
OTHER = "fedcba9876543210fedcba9876543210"
CREATED = 1_700_000_000_000


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path)


def started(store: RunStore, run_id: str = RUN) -> None:
    store.create(run_id, "basic-pair", "interrogation", CREATED)
    store.move(run_id, RunStatus.RUNNING)


def test_a_run_is_created_pending_and_then_ends_passed(store: RunStore) -> None:
    created = store.create(RUN, "basic-pair", "interrogation", CREATED)
    assert created.status is RunStatus.PENDING
    assert (created.events, created.dropped, created.ended_at_ms) == (0, 0, None)
    store.move(RUN, RunStatus.RUNNING)
    ended = store.move(RUN, RunStatus.PASSED, ended_at_ms=CREATED + 500)
    assert ended.status is RunStatus.PASSED
    assert ended.ended_at_ms == CREATED + 500
    assert store.get(RUN) == ended


def test_a_status_moves_only_along_the_model_transitions(store: RunStore) -> None:
    store.create(RUN, "basic-pair", "interrogation", CREATED)
    with pytest.raises(InvalidTransition, match="cannot be passed"):
        store.move(RUN, RunStatus.PASSED, ended_at_ms=CREATED)
    store.move(RUN, RunStatus.RUNNING)
    store.move(RUN, RunStatus.CANCELLED, ended_at_ms=CREATED)
    with pytest.raises(InvalidTransition):
        store.move(RUN, RunStatus.RUNNING)


def test_an_ended_status_needs_its_time_and_a_running_one_takes_none(
    store: RunStore,
) -> None:
    store.create(RUN, "basic-pair", "interrogation", CREATED)
    store.move(RUN, RunStatus.RUNNING)
    with pytest.raises(RunError, match="needs the time it ended"):
        store.move(RUN, RunStatus.FAILED)
    with pytest.raises(RunError, match="cannot end before it was created"):
        store.move(RUN, RunStatus.FAILED, ended_at_ms=CREATED - 1)
    store.create(OTHER, "basic-pair", "interrogation", CREATED)
    with pytest.raises(RunError, match="has not ended"):
        store.move(OTHER, RunStatus.RUNNING, ended_at_ms=CREATED)


def test_a_run_that_is_not_there_is_unknown(store: RunStore) -> None:
    with pytest.raises(UnknownRun):
        store.get(RUN)
    with pytest.raises(UnknownRun):
        store.get("../escape")


def test_a_run_is_created_once(store: RunStore) -> None:
    store.create(RUN, "basic-pair", "interrogation", CREATED)
    with pytest.raises(RunError, match="already exists"):
        store.create(RUN, "basic-pair", "interrogation", CREATED)
    with pytest.raises(RunError, match="not a run identity"):
        store.create("not-hex", "basic-pair", "interrogation", CREATED)


def test_events_are_numbered_in_order_and_paged(store: RunStore) -> None:
    started(store)
    for step in range(3):
        assert store.record(RUN, "verdict", {"step": step}) == step + 1
    first = store.events(RUN, limit=2)
    assert [event["seq"] for event in first.events] == [1, 2]
    assert first.events[0] == {"seq": 1, "kind": "verdict", "step": 0}
    assert first.next_cursor == 2
    second = store.events(RUN, after=first.next_cursor, limit=2)
    assert [event["seq"] for event in second.events] == [3]
    assert second.next_cursor is None


def test_the_journal_is_one_canonical_json_line_per_event(
    store: RunStore, tmp_path: Path
) -> None:
    started(store)
    store.record(RUN, "point", {"value": True, "address": 1})
    line = (tmp_path / "runs" / RUN / "events.jsonl").read_text(encoding="utf-8")
    assert line == '{"address":1,"kind":"point","seq":1,"value":true}\n'
    assert json.loads(line) == {"seq": 1, "kind": "point", "address": 1, "value": True}


def test_the_store_keeps_the_fields_it_owns(store: RunStore) -> None:
    started(store)
    with pytest.raises(ValueError, match="do not pass them"):
        store.record(RUN, "point", {"seq": 9})


def test_a_full_journal_drops_the_next_events_and_counts_them(tmp_path: Path) -> None:
    store = RunStore(tmp_path, max_events=2)
    started(store)
    assert store.record(RUN, "point", {"n": 1}) == 1
    assert store.record(RUN, "point", {"n": 2}) == 2
    assert store.record(RUN, "point", {"n": 3}) is None
    assert store.record(RUN, "point", {"n": 4}) is None
    record = store.get(RUN)
    assert (record.events, record.dropped) == (2, 2)
    assert [event["n"] for event in store.events(RUN).events] == [1, 2]


def test_a_run_that_has_ended_takes_no_more_events(store: RunStore) -> None:
    started(store)
    store.move(RUN, RunStatus.PASSED, ended_at_ms=CREATED + 1)
    with pytest.raises(RunEnded):
        store.record(RUN, "point", {"n": 1})


def test_an_artifact_is_stored_once_with_its_digest(store: RunStore) -> None:
    started(store)
    data = b"<testsuites/>"
    stored = store.add_artifact(RUN, "junit.xml", data)
    assert stored.sha256 == hashlib.sha256(data).hexdigest()
    assert stored.size == len(data)
    assert store.artifacts(RUN) == [stored]
    assert store.read_artifact(RUN, "junit.xml") == data
    with pytest.raises(ArtifactRefused, match="already exists"):
        store.add_artifact(RUN, "junit.xml", b"other")


@pytest.mark.parametrize("name", ["../escape", "Junit.xml", "a/b", ""])
def test_an_artifact_name_is_a_plain_file_name(store: RunStore, name: str) -> None:
    started(store)
    with pytest.raises(ArtifactRefused, match="not a valid artifact name"):
        store.add_artifact(RUN, name, b"x")


def test_an_artifact_larger_than_the_limit_is_refused(tmp_path: Path) -> None:
    store = RunStore(tmp_path, max_artifact_bytes=4)
    started(store)
    store.add_artifact(RUN, "small", b"1234")
    with pytest.raises(ArtifactRefused, match="the limit is 4"):
        store.add_artifact(RUN, "large", b"12345")


def test_an_artifact_that_is_not_there_is_unknown(store: RunStore) -> None:
    started(store)
    with pytest.raises(UnknownRun, match="no artifact named 'missing'"):
        store.read_artifact(RUN, "missing")


def test_runs_are_listed_in_creation_order_in_pages(store: RunStore) -> None:
    for run_id in (RUN, OTHER, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"):
        store.create(run_id, "basic-pair", "interrogation", CREATED)
    first = store.list_runs(limit=2)
    assert [run.id for run in first.items] == [RUN, OTHER]
    assert first.next_cursor is not None
    second = store.list_runs(limit=2, cursor=first.next_cursor)
    assert [run.id for run in second.items] == ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
    assert second.next_cursor is None


def test_a_page_size_out_of_range_is_refused(store: RunStore) -> None:
    with pytest.raises(RunError, match="the page size is 0"):
        store.list_runs(limit=0)


def test_the_records_survive_a_reopen(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    started(store)
    store.record(RUN, "point", {"n": 1})
    store.add_artifact(RUN, "report.json", b"{}")
    store.move(RUN, RunStatus.PASSED, ended_at_ms=CREATED + 9)
    store.close()

    reopened = RunStore(tmp_path)
    record = reopened.get(RUN)
    assert record.status is RunStatus.PASSED
    assert record.events == 1
    assert [event["n"] for event in reopened.events(RUN).events] == [1]
    assert reopened.read_artifact(RUN, "report.json") == b"{}"


def test_a_store_written_by_a_newer_version_is_refused(tmp_path: Path) -> None:
    RunStore(tmp_path).close()
    import sqlite3

    connection = sqlite3.connect(tmp_path / "runs.sqlite3")
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()
    with pytest.raises(RunError, match="use a newer server"):
        RunStore(tmp_path)
