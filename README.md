# igs-iec104-mcp

An MCP server that defines, starts, and runs IEC 60870-5-104 test benches on Docker or Podman. A bench is a
set of protocol runtimes built on the `igs-iec104` Rust library: stations, clients, and redundancy groups on
their own networks. A scenario drives a bench through its control APIs and records every verdict, every event,
and the evidence of each run.

The server is a Python 3.12 program that speaks MCP over stdio, or over Streamable HTTP on the loopback. The protocol runtime is a Rust program, packaged
in a container image. The protocol work is done by the upstream library, pinned by revision (`runtime/Cargo.toml`).

## Documentation

| Document | What it covers |
| --- | --- |
| [Setup](docs/setup.md) | Prerequisites, building the images, starting the server, connecting an MCP client |
| [Install with a coding agent](docs/install-agent.md) | The entry point for an agent: the steps, the output to expect, and when to ask a person |
| [Definitions](docs/definitions.md) | The bench and scenario YAML, the actions, the schemas |
| [Docker benches](docs/docker.md) | Networks, connectivity, published stations, redundancy groups, recovery |
| [Tools](docs/tools.md) | The MCP tools, their error codes, the run states and verdict outcomes |
| [Runs and artifacts](docs/runs-and-artifacts.md) | What a run keeps, the reports, retention, secrets |
| [Troubleshooting](docs/troubleshooting.md) | Symptoms, causes, and fixes |
| [Platform matrix](docs/platform-matrix.md) | The platforms tested, the feature support, and the limitations |

The project's design and its task history are in `IMPLEMENTATION.md` and `DECISIONS.md`; the per-task logs are
in `docs/ai-log/`.

## Status

Milestones 1 to 7 are done, and milestone 8 (the validation release) is in progress. Implemented: bench definitions,
the Docker and Podman lifecycle, the scenario engine, the MCP tools over stdio and over Streamable HTTP on the loopback
with a bearer token, the external target policy, published server endpoints, reports and retention, TLS on both roles,
redundancy groups, and controlled faults (latency and interrupt). Interoperability is checked against an independent
implementation (OpenMUC j60870) in a test-only image. The release documentation and the platform matrix are in
progress (V4): the Docker Desktop check is still pending. The release itself (V5) is a maintainer decision. The
remaining work is listed in the platform matrix. The server is not a certified implementation, and its reports do not
certify the protocol behaviour of any station or client.

## License

Apache License 2.0. See [LICENSE](LICENSE).
