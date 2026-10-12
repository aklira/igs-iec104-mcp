# Docker benches

A scenario runs on a bench: a set of containers started for one run, removed when the run ends. This page
describes what the engine gets, how the containers reach each other and the controller, and how a bench is
recovered after a crash. Docker Engine and Podman are both supported; the [platform matrix](platform-matrix.md)
lists what has been tested.

## What a bench gets

For each run, the server starts:

- one **network** for each network of the bench. A network is internal (no route outside the host) unless the
  bench declares it external;
- one **runtime container** for each instance. A station and a client run the same program in different roles;
- one **relay container** for each runtime. A relay is a small Python program that forwards the runtime's control
  API to the controller. A published station also gets a second relay, for its IEC 104 port (see
  [published stations](#published-stations)).

Every runtime container runs with:

- its own fixed, non-root user (10001); the relay runs as 10002;
- no privileged mode, no added capabilities (all are dropped), and `no-new-privileges`;
- the limits of its instance: CPU, memory, processes, and the event buffer (see
  [definitions](definitions.md#container-limits));
- no bind mount and no Docker socket.

The controller never mounts a host directory into a container. The files a runtime needs (its points and its
control token) are copied into the container through the engine's archive interface before it starts, so the
token is never in the container's environment, in a mount, or in the evidence.

## How the containers reach each other

Inside a bench, a client finds its station by the station's name on the bench network. The server does not pick
host ports for that traffic, and the runtime binds its own ports: a port is never chosen on the host and released
before the process binds it.

The controller reaches each runtime's control API through its relay. The relay publishes the control port on the
loopback (`127.0.0.1`) at a port the engine chooses, and the controller reads that port back from the engine. The
control API needs the bench's token, so a request from another process on the host is refused.

## Published stations

A station that an external client must reach can be published. The definition names the port inside the bench, the
host port, the bind address, and the endpoint the client is given:

```yaml
instances:
  - name: server-1
    role: server
    network: lan
    common_address: 1
    publish:
      port: 2404
      host_port: 2404
      bind: 127.0.0.1
      advertise: lab-host.example
```

- `port` is the station's port in the bench. The runtime listens on it, so it is imposed, not chosen.
- `bind` defaults to `127.0.0.1`. A wider address is an explicit choice in the definition, and it is the only way
  the port is reachable from another host.
- `advertise` is the host name or address the external client connects to. It is declared, never discovered.

An external client connects to `advertise:host_port`. The station's relay listens on the port inside the bench, and
the engine publishes that relay on `bind:host_port`.

## Fault proxies

A connection that declares `fault_proxy: true` is carried by a fault proxy: a program of the relay image, with its own
container on the connection's network, and its own control relay like a runtime's. The proxy listens on port 2404
inside its container, and the client is given the proxy's address and that port. The proxy is started after the
station is ready, since it forwards to the station's bound address. The token is copied in through the archive
interface, and the proxy runs as the relay's user, with no privilege and no mount.

On stop, the proxy's logs are kept as `proxy-<n>`, and its exit code is not counted in the graceful-stop check, as a
relay's is not.

## Redundancy groups

A client connected to several stations forms a redundancy group, up to eight stations. The group starts the data
transfer of the first station that connects, and when that station is lost it starts another connected station.
The `switchover` and `stop_instance` steps test this; see [scenarios](definitions.md#actions).

The planner gives each station a position in the group, in the order the bench names its connections. The events
of a group name the station by that position (`endpoint`).

## Managed instances and their stop

A managed instance can be stopped by a scenario, for good: its container gets SIGTERM with a grace of ten seconds,
and its logs and record are kept in the run's evidence as `<instance>-stopped`. The container stays until the bench
is removed.

A stopped instance is not started again. `start_instance` is refused on a managed bench, because a restart needs a
fixed station port, which the plan does not impose yet.

## External targets

An external target is a station or a client that the bench does not manage. Its definition is validated and its
policy is enforced (see [definitions](definitions.md#external-targets)), but the planner refuses a connection that
reaches one: a bench cannot run a scenario against an external target yet. A bench that declares an external
network can therefore be validated, but a run that needs the connection is refused before the containers start.

## Ownership, labels, and removal

Each resource the server creates carries these labels: `dev.igs-iec104.mcp.managed`, `.project`, `.bench`, `.run`,
and `.instance`. The server removes only the resources that carry its labels for the bench being removed. A
partial creation is rolled back, and its logs and records are kept first.

## Recovery after a crash

If the server stops while a run is in progress, the next start marks that run `interrupted` and does not resume
it. Its bench, if the engine still holds one, is listed by `bench_recovered`, which names each bench by its run.
`bench_recovered_remove` removes the resources of one recovered bench; its logs and records are kept as evidence
first. Nothing is removed automatically, and no command of the interrupted run is replayed.

## Engine requirements

- The engine must be reachable through `DOCKER_HOST` or its default socket. `scenario_run` checks it before it creates
  a run, and a call made while the engine is unavailable is refused with the error code `engine_unavailable`.
- The runtime and relay images are given to the server by identity (see
  [setup](setup.md#build-the-runtime-and-the-images)). The server starts containers only from those images.
- The engine must allow the containers' limits (CPU, memory, processes), since they are enforced by the engine.
