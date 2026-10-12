# Prompt examples

These prompts show what the MCP server is for. Each one names the tools an assistant should call, so that you can
check its work. They assume the server is connected as [setup](setup.md) describes, and that the project holds the
example bench `basic-pair` and its scenario `interrogation-and-spontaneous-update`, or can receive them.

The examples use the files of the repository: `examples/benches/basic-pair.yaml` and
`examples/scenarios/interrogation-and-spontaneous-update.yaml`. The other names and values in the prompts are
illustrations.

Ask for a confirmation before any run: `scenario_run` starts containers on your machine, and an assistant that is not
asked to stop first may start one at once.

## 1. Look at the project

> List the benches and the scenarios of the project, with their counts.

Tools: `bench_list`, `scenario_list`. A good first step in any session.

## 2. Validate a definition without storing it

> Validate this bench and tell me its problems. Do not create it: [paste a bench with a wrong port or a missing
> `common_address`].

Tool: `bench_validate`. Nothing is written. Each problem comes with its path in the YAML, which is enough to correct
the definition before it is stored.

## 3. Create the example bench and its scenario

> Use `examples/benches/basic-pair.yaml`. Validate it, then create it as `basic-pair`. Do the same with
> `examples/scenarios/interrogation-and-spontaneous-update.yaml`.

Tools: `bench_validate`, `bench_create`, `scenario_validate`, `scenario_create`.

## 4. Change a scenario without overwriting someone else's change

> Read the scenario `interrogation-and-spontaneous-update`, change the timeout of its `update_point` step to 10 s, and
> save it with the revision you just read.

Tools: `scenario_get`, then `scenario_update` with `revision`. If the stored scenario changed since it was read, the
`stale_revision` error asks the assistant to read it again before it writes.

## 5. Run a scenario and follow it

> Run the scenario `interrogation-and-spontaneous-update`. Then report its status every few seconds until it ends, and
> name the step that failed, if one did.

Tools: `scenario_run`, `run_get`, `run_events`. The run is returned at once with the status `running`.

## 6. Produce a report for a continuous integration system

> For the finished run, give me the report in JUnit format and save it to `reports/basic-pair.xml`.

Tool: `run_report` with `format` set to `junit`. The report is stored with the run. Saving it to a file is up to the
client, which needs file access for that.

## 7. Test a redundancy switchover

> Create a bench where `client-1` is connected to two stations, `server-a` and `server-b`. Write a scenario that starts
> the transfer, waits for a general interrogation, stops the active station, and checks the switch to the other
> station with the `switchover` action. Validate everything before you create it.

Tools: `bench_validate`, `bench_create`, `scenario_validate`, `scenario_create`, then `scenario_run`. A redundancy group
takes up to eight stations, and the validation must pass before the run.

## 8. Inject a controlled fault

> Add `fault_proxy: true` to the connection from `client-1` to `server-1`. In the scenario, interrupt that connection
> with `inject_fault` in `interrupt` mode, wait until the client is down, then call `clear_fault` and check that the
> client reconnects. Then add a latency of 300 ms in `latency` mode.

Tools: `bench_create` (the connection flag), `scenario_create`, `scenario_run`, `run_events`. A fault step on a
connection without the flag is refused at validation, which is a useful check of the validator.

## 9. Check that a station refuses a connection

> Write a scenario that expects the TLS station to refuse a client presenting a wrong certificate. The certificate
> material stays outside the project, in the secrets directory.

Tools: `scenario_create`, `scenario_run`. The `expect_refusal` action accepts a TLS alert or a refused connection.
The certificates themselves must not appear in the conversation.

## 10. Check an external target without contacting it

> Validate the external target `plc-lab-3` with `target_validate`, without a probe. Tell me whether its allowed
> operations cover the commands of the scenario.

Tool: `target_validate` with `probe` set to `false`. No IEC frame is sent. A command to a managed instance of the bench
is unsupported for now.

## 11. Clean up the project

> Show me what `run_prune` would remove with the current policy, without removing anything. Then list the benches left
> by runs that did not end.

Tools: `run_prune` (a dry run by default), `bench_recovered`. Removing a run is a separate request, and
`bench_recovered_remove` keeps the logs as evidence before it removes the resources.

## 12. Cancel a run that is stuck

> The current run seems stuck for more than two minutes. Cancel it, then tell me its final status and its last events.

Tools: `run_get`, `run_cancel`, `run_events`. The run becomes `cancelled` once its steps have stopped and its cleanup
has run.
