# Runs and artifacts

A run keeps what it needs to be read after its bench is gone: its status and times, its journal of events, its
inputs, its evidence from the containers, and its reports. This page says where each is kept, how long, and what is
never written down.

## Layout of the project directory

```text
<project>/
  benches/<name>.yaml            stored benches
  scenarios/<name>.yaml          stored scenarios
  runs.sqlite3                   run records: status, times, counts, and the artifact index
  runs/<run-id>/events.jsonl     the event journal, one JSON object per line
  runs/<run-id>/snapshot.json    the inputs of the run, as they were when it started
  runs/<run-id>/artifacts/       the reports of the run
  runs/<run-id>/evidence/        the logs and records of the containers
```

The project directory is meant to be versioned. The run records are not: they are local output, and a run's files
are removed only by the retention policy below.

## The journal

The journal records each verdict, gap, confirmation, and engine record as it happens, so a client can follow a run
before it ends. It is bounded: `--max-events` (10000 by default) is the number of events it keeps. An event past
that limit is counted as dropped and not kept, and the run records how many.

A runtime's own event log is bounded too (`event_buffer` of the instance, 10000 by default). When that log drops an
event, the run records a **gap** with the instance's name and the number dropped. A gap is also recorded when a
runtime's drop count cannot be read at all. The report lists the gaps, so a reader knows the run may lack some
evidence of that instance. See [definitions](definitions.md#container-limits) for the setting.

## Snapshots

When a run starts, its inputs are fixed: the SHA-256 of the bench and scenario texts, and the digests of the secrets
the bench names. Editing a stored definition does not change a run in progress, and a report says which inputs the
run used.

## Evidence

The evidence is what the containers wrote, kept before they are removed:

| File | Content |
| --- | --- |
| `<instance>.log`, `<instance>.json` | a runtime's logs, and its engine record (state, ports, networks) |
| `<instance>-stopped.log`, `<instance>-stopped.json` | the same for an instance a scenario stopped, kept at the moment of its stop |
| `relay-<instance>.log`, `relay-<instance>.json` | the relay of a runtime |
| `publication-<instance>.log`, `publication-<instance>.json` | the relay of a published station's IEC port |
| `stop.json` | the exit code of each runtime, whether every stop was graceful, and how many resources were removed |

Evidence is kept when a bench is removed, after success and after failure. A bench that fails to start keeps the
logs and records of the containers it created, before its rollback removes them.

The control token is never in the evidence, the journal, the snapshot, or a report. The engine records of the
containers do not carry it either, because it is copied in through the archive interface, not through the
environment.

## Reports

`run_report` makes a report of an ended run, in one of three formats:

| Format | Artifact | Use |
| --- | --- | --- |
| `json` | `report.json` | the whole document: the run, its inputs, its engine identities, each step's outcome and category, and the notes |
| `junit` | `report.junit.xml` | a JUnit XML with one test case per step; a failed step is a failure, an unsupported one is skipped |
| `summary` | `summary.md` | a Markdown summary: the verdicts, the steps, the failures and gaps, the cleanup, the confirmations, and the manifest of the evidence and artifacts |

A report is made the first time it is asked for, and stored as an artifact of its run. Later requests read the stored
copy, so a report does not change. A run that has not ended has no report: `invalid_state` is returned, because its
report would change as the run goes on.

Each step's outcome is sorted into a category: `passed`, `assertion_failure`, `infrastructure_error`, `timeout`,
`unsupported`, `cancelled`, or `indeterminate`. The report also gives the upstream library's revision, the runtime
and relay identities, and the digests of the inputs.

An artifact is written once. Its SHA-256 is recorded, and an artifact larger than 16 MiB is refused.

## Retention

Runs accumulate, so the server can remove the ended ones. Start it with a policy, by count, by age, or both:

```bash
PYTHONPATH=src .venv/bin/python -m igs_iec104_mcp --project ~/igs-project --keep-runs 50 --keep-days 30
```

`run_prune` applies the policy. Its `dry_run` defaults to true: the first call reports the runs it would remove, and
the second, with `dry_run` false, removes them. A prune without a policy is refused with `invalid_argument`.

The policy never selects a run that has not ended, nor a run whose bench is still on the engine. A removed run leaves
no trace: its record, its journal, its artifacts, and its evidence are all deleted.

## Secrets

The secrets are the certificates and keys a bench names with `secret:<name>`. They live in a directory the server is
given with `--secrets-dir`, and that directory must follow these rules, which the server checks:

- it is outside the project directory, which is versioned;
- it is readable by its owner only (`chmod 700`);
- each secret is one regular file, readable by its owner only (`chmod 600`), not a symbolic link;
- each secret is at most 64 KiB.

The bytes are read only when a bench starts. A run records the SHA-256 of each secret it read, never the content.
The errors name the secret and the rule it breaks, never its content.

A definition never holds key material. It holds only the reference.
