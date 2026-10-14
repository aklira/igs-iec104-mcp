# Definitions

A bench and a scenario are YAML documents. The server validates each one before it stores it, and reports every
problem with its path, so a definition is fixed in one pass. The JSON Schemas of the definitions are committed in
[`contracts/definitions/`](../contracts/definitions/); they are generated from the models and checked in CI.

Both documents start with `schema_version: 1`. Unknown fields are refused, with their path. A name is lowercase: a
letter, then up to 62 lowercase letters, digits, or hyphens.

Durations are written as `500ms`, `10s`, `5m`, or `1h`. A step's timeout is at most an hour. The whole scenario may
set a `global_timeout` of at most a day.

## The bench

A bench is one network or more, the instances on them, the external targets it reaches, and the connections
between clients and stations.

```yaml
schema_version: 1
name: basic-pair
networks:
  - name: lan
instances:
  - name: client-1
    role: client
    network: lan
    common_address: 1
  - name: server-1
    role: server
    network: lan
    common_address: 1
    points:
      - address: 100
        kind: float
        value: 12.5
connections:
  - client: client-1
    server: server-1
```

The full example is [`examples/benches/basic-pair.yaml`](../examples/benches/basic-pair.yaml).

| Field | Meaning |
| --- | --- |
| `networks[].name`, `networks[].external` | a network of the bench; an external one may reach an external target |
| `instances[].name`, `role` | `client` or `server`; the name is what steps and connections use |
| `instances[].network` | the network the instance is on |
| `instances[].common_address` | the common address, 1 to 65534 (65535 is the global address) |
| `instances[].parameters` | the link parameters: `t0`, `t1`, `t2`, `t3` in seconds, and `k`, `w`. Defaults: 30, 15, 10, 20, 12, 8 |
| `instances[].points` | a station's points (below) |
| `instances[].limits` | the container limits (below) |
| `instances[].publish` | a station's port made reachable from outside Docker (see [Docker benches](docker.md#published-stations)) |
| `instances[].tls` | the TLS material of an instance, as secret references (see [TLS](#tls)) |
| `external_targets[]` | a station or client the bench does not manage (see [External targets](#external-targets)) |
| `connections[].client`, `server` | a client connects to a station; a client may connect to several stations, which form its group |
| `connections[].fault_proxy` | the connection goes through a fault proxy, which a scenario can interrupt or delay (see [faults](#faults)) |
| `cleanup.remove`, `cleanup.keep_evidence` | when the resources are removed, and whether the evidence is kept (below) |

### Points

A point has an `address`, a `kind`, and a `value`. The kinds are the monitoring kinds of the upstream library:
`single`, `double`, `step`, `bitstring`, `normalized`, `scaled`, `float`, `counter`, `packed`, and
`normalized_unqualified`. Each kind takes a value of its own type; the validator checks it.

Optional fields of a point:

- `stamped: true` sends the point with a time tag. It is refused for the kinds with no time-tagged form (`packed`
  and `normalized_unqualified`).
- `command` says how the station answers a command to the point: `accept`, `refuse`, or `unknown_address`
  (the default). The policy is known in advance, because the upstream handler decides synchronously.
- `simulation` changes the value at each period: a `kind` of `toggle`, `ramp`, or `walk`, with a `period_ms` and an
  optional `increment`. Each change is a function of the step and a seed, so a run can be repeated exactly.

### Container limits

Each instance's container has these limits. Their defaults apply when a field is absent.

| Field | Default | Range |
| --- | --- | --- |
| `cpu` (cores) | 0.5 | above 0 and up to 8 |
| `memory_mb` | 256 | 16 to 4096 |
| `pids` | 64 | 1 to 512 |
| `event_buffer` (events kept for the run) | 10000 | 1 to 1000000 |
| `execution_seconds` | 3600 | 1 to 86400 |

The engine enforces the first three. The runtime keeps `event_buffer` events, and the run keeps the drops of
these events as gaps (see [runs and artifacts](runs-and-artifacts.md#the-journal)).

### Cleanup

The runner carries out the default policy only: `remove: success_and_failure` with `keep_evidence: true`. A run
whose bench declares another policy is refused when it starts, with the error code `unsupported`, rather than run
with a policy the runner does not keep.

### TLS

An instance names its certificate, key, trust root, and intermediates as secret references, `secret:<name>`.
The bytes are read from the secrets directory when a bench starts, and never stored in the definition.

- `certificate`, `key`, and `trust` are required with `tls`. `intermediates` takes up to eight references, in
  chain order.
- `server_name` belongs to a client: the name it checks the station's certificate against. A station does not
  take one.

Both roles use the upstream TLS settings. A station that refuses a client's certificate records a security alarm
(see [scenarios](#scenarios)).

## External targets

An external target is a station or a client that the bench does not manage. The server never restarts it,
reconfigures it, or destroys it.

```yaml
external_targets:
  - name: plant
    role: server
    network: wan
    endpoint:
      host: 192.0.2.10
      port: 2404
    common_address: 1
    allowed_operations: [interrogate, read]
```

- `allowed_operations` lists the protocol operations a scenario may ask for. A `send_command` to a target that does
  not list `command` fails validation (`undeclared`). The other operations are not checked at validation yet.
- A state-changing operation (`command`, `setpoint`, `clock_sync`, `counter_freeze`, `counter_reset`) needs a
  person's confirmation at run time, given through MCP elicitation. A client that cannot be asked needs an `opt_in`
  with a `reviewer`, a `reviewed_on` date (`YYYY-MM-DD`), and the operations it covers.
- `target_validate` checks the definition without connecting. Its `probe` is explicit and sends no IEC frame.

Connecting a bench to an external target is not supported yet: the planner refuses a connection that reaches one,
so a scenario cannot run against it. The policy above is in place and tested.

## Scenarios

A scenario runs against one bench. Its steps run in order, and an optional `cleanup` list runs after success,
failure, or cancellation.

```yaml
schema_version: 1
name: interrogation-and-spontaneous-update
bench: basic-pair
steps:
  - action: start_transfer
    client: client-1
    timeout: 10s
  - action: general_interrogation
    client: client-1
    timeout: 5s
  - action: assert_point
    client: client-1
    address: 100
    kind: float
    value: 12.5
    tolerance: 0.001
  - action: update_point
    server: server-1
    address: 100
    value: 18.0
    timeout: 5s
  - action: assert_point
    client: client-1
    address: 100
    kind: float
    value: 18.0
    tolerance: 0.001
    cause: 3
```

The full example is [`examples/scenarios/interrogation-and-spontaneous-update.yaml`](../examples/scenarios/interrogation-and-spontaneous-update.yaml).

### Actions

| Action | What it does |
| --- | --- |
| `wait_connection` | waits until a client is connected |
| `start_transfer` | requests the data transfer of a client and waits until it is started |
| `wait_transfer_started` | waits until the transfer of a client is started |
| `general_interrogation` | interrogates a client and waits for the termination |
| `assert_point` | checks the value of a point in a client's image, once |
| `expect_point` | waits for a point observation after the step started that meets the step's conditions, within the timeout |
| `update_point` | sets a station's point and waits until the station and its clients hold the value |
| `send_command` | a command to a point of an external target: confirmed by a person first, then unsupported until external connections are made. A command to a managed instance is unsupported for now |
| `expect_refusal` | expects a station to refuse a client's connection: a TLS alarm, or a refused connect |
| `stop_instance` | stops a managed instance for good; its logs are kept, and each client records the loss |
| `switchover` | stops the station a redundancy group started, and waits for the switch to another station |
| `inject_fault` | on a connection through a proxy: `interrupt` closes its connections and refuses the reconnections; `latency` delays each chunk by `latency_ms` |
| `clear_fault` | on a connection through a proxy: the traffic flows again, and an interrupted client reconnects |
| `start_instance` | refused on a managed bench: a restart needs a fixed station port |
| `parallel` | runs branches of steps at once; the step ends when every branch has ended |

Each step may name a `timeout`. A step that does not pass by its deadline has the outcome `timed_out`. A scenario
may also set a `global_timeout`; when it passes, the run fails and its cleanup steps still run.

The exact fields of each action are in the scenario schema (`contracts/definitions/scenario.schema.json`).

### One station, several clients

A station serves the data of one client at a time: the client that started its transfer last. A `start_transfer`
of a client closes the other clients of that station that are not stopped (see [Docker benches](docker.md#one-station-several-clients)).
The check of a scenario follows the order of its steps, and refuses a `general_interrogation` or an `expect_point`
of a client that another client has superseded. Its message names both clients and the station:

```text
steps[2]: 'client-1' was superseded by 'client-2' on 'server-1': a station serves one client's transfer at a time, so this step gets no answer. Run it before 'client-2' starts its transfer; starting 'client-1' again closes 'client-2' in turn
```

The check judges only what the bench makes certain. A client that connects to a redundancy group, and a station that
a group connects to, are not judged; neither are the steps of a `parallel` step. A stop or a start of an instance, and
a fault on a connection, end what the check knows about that client, so it does not refuse a later step on them.

### Assertions and cursors

An assertion looks at what happens after its step starts, not at earlier events: the events are read from a cursor
taken when the step starts. A `cause` in an assertion matches the cause of transmission of the event that carries
the value (for example `3` for a spontaneous update).

### Snapshots

A run takes a snapshot of its inputs when it starts: the bench and scenario texts (by SHA-256), and the digests of
the secrets it reads. Editing a definition does not change a run in progress.

## Faults

A connection that declares `fault_proxy: true` goes through a proxy on the bench network: the client connects to the
proxy, which forwards every byte to the station. Both ends must be managed instances on one network.

```yaml
connections:
  - client: client-1
    server: server-1
    fault_proxy: true
```

- `inject_fault` with `mode: interrupt` passes when the client's connection is down. The proxy refuses the
  reconnections until `clear_fault`, so the client stays down until then.
- `inject_fault` with `mode: latency` and a `latency_ms` (1 to 60000) passes when the proxy reports that mode. The delay
  applies to each chunk the proxy forwards, in both directions.
- A fault step on a connection without the flag is refused at validation.

The proxy changes no byte and drops none. Its own control API needs the bench's token, as a runtime's does.

## Validation and schemas

| Tool | Purpose |
| --- | --- |
| `bench_validate`, `scenario_validate` | check a text without storing it; every problem is listed with its path |
| `target_validate` | check an external target of a stored bench, without connecting |

The schemas are regenerated from the models with:

```bash
PYTHONPATH=src .venv/bin/python -m igs_iec104_mcp.models.schemas --write
```

and checked with `--check`. The repository's tests fail when a committed schema is out of date.
