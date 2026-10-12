# MCP tools

The server exposes 19 tools, over stdio or over the loopback HTTP transport. Their descriptions are what the client shows; this page gives the contracts
behind them: the arguments, the results, the error codes, and the states a run moves through.

Every tool returns its result as JSON. A failed call says its error code first, then a message that names what to
change.

## Definitions

| Tool | Arguments | Result |
| --- | --- | --- |
| `bench_validate` | `text` | the problems of a bench text, each with its path; nothing is stored |
| `bench_create` | `name`, `text` | the stored bench, with its revision; refused when the name is taken |
| `bench_get` | `name` | the bench with its revision and its text |
| `bench_list` | `limit` (default 50, at most 200), `cursor` | a page of bench names in order, and `next_cursor` |
| `scenario_validate` | `text` | the problems of a scenario text; when the project holds its bench, the steps are checked against it |
| `scenario_create` | `name`, `text` | the stored scenario; refused when the name is taken |
| `scenario_update` | `name`, `text`, `revision` | the replaced scenario; `revision` is the one it was read at |
| `scenario_get` | `name` | the scenario with its revision and its text |
| `scenario_list` | `limit`, `cursor` | a page of scenario names, as `bench_list` |
| `target_validate` | `bench`, `target`, `probe` (default false), `timeout` | the external target's checks; `probe` opens a connection and sends no IEC frame |

There is no `bench_update` tool. A changed bench is stored under a new name. A scenario is updated with
`scenario_update`, which refuses a write when the file changed since `revision` was read.

## Runs

| Tool | Arguments | Result |
| --- | --- | --- |
| `scenario_run` | `name` | the new run, at once, with status `running`; the bench starts and the scenario runs in the background |
| `run_get` | `run_id` | the run: status, times, and the counts of its events and drops |
| `run_list` | `limit`, `cursor` | a page of runs, in creation order |
| `run_events` | `run_id`, `limit` (default 50), `cursor` | a page of the run's journal, in order: verdicts, gaps, confirmations, and the rest |
| `run_report` | `run_id`, `format` (`json`, `junit`, or `summary`) | the report of an ended run; it is stored with the run |
| `run_cancel` | `run_id` | asks a running run to stop; it ends `cancelled` once its steps have stopped and its cleanup has run |
| `run_prune` | `dry_run` (default true) | the ended runs the retention policy selects, and the ones kept; removes them only when `dry_run` is false |

A run is followed with `run_get` and `run_events`. The journal is read by its cursor, so a client that reads it
again sees each event once.

A command to an external target is confirmed by a person through the client, when the client supports MCP
elicitation. Without that support, the command is refused unless the target has a reviewed opt-in. Each decision is
recorded in the run's journal as a `confirmation` event.

## Recovery

| Tool | Arguments | Result |
| --- | --- | --- |
| `bench_recovered` | none | the benches the engine still holds for runs that did not end, each named by its run |
| `bench_recovered_remove` | `run_id` | removes the resources of one recovered bench; its logs and records are kept as evidence first |

See [Docker benches](docker.md#recovery-after-a-crash) for when these apply.

## Error codes

| Code | Meaning |
| --- | --- |
| `invalid_definition` | a bench or scenario text does not validate; the result lists the problems |
| `invalid_name` | a name is not valid, or it would lead outside the project directory |
| `not_found` | no definition or run has that name or identifier |
| `already_exists` | the name is taken |
| `stale_revision` | a scenario update names a revision that is no longer the stored one: read it again |
| `invalid_argument` | an argument is out of range or not allowed, such as an unknown report format |
| `storage_error` | the project directory could not be read or written; the message says what failed |
| `invalid_state` | the run is not in a state that allows the request, such as a report of a run that has not ended |
| `unsupported` | the bench asks for something the runner does not carry out, such as a cleanup policy other than the default |
| `engine_unavailable` | the Docker or Podman engine cannot be reached |

## Run states

A run moves along these states only; an ended run moves nowhere.

| State | Meaning |
| --- | --- |
| `pending` | created, not started |
| `running` | the bench is starting or the scenario is running |
| `passed` | every step passed |
| `failed` | a step failed, timed out, or was indeterminate, or the global timeout passed |
| `cancelled` | the run was cancelled |
| `interrupted` | the server stopped during the run; the run is not resumed |
| `error` | the bench did not start or stop, or a step could not run (an error or an unsupported step) |

## Verdict outcomes

Each step has one verdict.

| Outcome | Meaning |
| --- | --- |
| `passed` | the step's expectation held |
| `failed` | the expectation did not hold, for example a value or a cause that differs |
| `timed_out` | the deadline passed before the expectation held |
| `cancelled` | the run was cancelled during the step |
| `indeterminate` | a request was sent, and the connection ended before its answer; the request is never sent again |
| `unsupported` | the step asks for something the runtime or the bench does not support |
| `error` | the step could not run for a reason of the controller, such as a refused operation on a managed bench |

A run whose verdicts include an `error` or an `unsupported` ends `error`; any other outcome that is not `passed` makes
it `failed`.
