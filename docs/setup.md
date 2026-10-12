# Setup

This page takes a machine from nothing to a running MCP server with its images built. Commands are shown for
Linux; on Windows, run them inside WSL2 with a Linux engine.

## Prerequisites

| Tool | Version | Used for |
| --- | --- | --- |
| Python | 3.12 (`.python-version`) | the MCP server and the tests |
| [uv](https://docs.astral.sh/uv/) | any recent release | the locked Python environment |
| Rust | 1.99.0 (`runtime/rust-toolchain.toml`) | building the runtime |
| OpenSSL development files and `pkg-config` | system packages | the runtime's TLS backend |
| Docker Engine, or Podman 5 with its Docker-compatible socket | see [the platform matrix](platform-matrix.md) | the benches |
| `openssl` command line | any | the TLS tests only |

Install the Python environment from the lock file:

```bash
uv sync --locked
```

## Build the runtime and the images

Build the runtime binary (the integration tests use it for the local bench):

```bash
cd runtime
cargo build --locked --release --package igs-iec104-runtime
cd ..
```

The binary is `runtime/target/release/igs-iec104-runtime`: the Cargo workspace is `runtime/`, so its target directory is there.

Build the two images. Each takes its own directory as the build context:

```bash
docker build --file docker/runtime.Dockerfile --tag igs-iec104-runtime:local runtime
docker build --file docker/relay.Dockerfile --tag igs-iec104-relay:local docker
```

The server takes each image by its identity, not by its tag, so that a bench runs exactly the image that was
checked. Read the identities:

```bash
docker image inspect --format '{{.Id}}' igs-iec104-runtime:local
docker image inspect --format '{{.Id}}' igs-iec104-relay:local
```

Each command prints a `sha256:` value. Keep both: they are the values of `--runtime-image` and `--relay-image`.

## Create a project directory

The project directory holds the definitions and the run records. It must exist, and it should be versioned, so
that the definitions are reviewed like the rest of the code:

```bash
mkdir -p ~/igs-project
```

The server creates `benches/`, `scenarios/`, `runs/`, and `runs.sqlite3` inside it as it needs them. Scenario files written by the
server are plain YAML files; nothing is committed or pushed automatically.

Keep the secrets (TLS certificates and keys) outside the project, in a directory that only the owner can read.
See [runs and artifacts](runs-and-artifacts.md#secrets) for the rules.

## Start the server

By default the server speaks MCP over stdio: it is started by an MCP client, not run in a terminal. Its standard
output carries the MCP messages only, and its diagnostics go to standard error. The HTTP transport is the other
option; see [HTTP transport](#http-transport).

Check the options from the repository:

```bash
PYTHONPATH=src .venv/bin/python -m igs_iec104_mcp --help
```

| Option | Meaning |
| --- | --- |
| `--project DIR` | the project directory, required; it must exist |
| `--runtime-image ID` | the runtime image identity, needed to run a bench |
| `--relay-image ID` | the relay image identity, needed to run a bench |
| `--secrets-dir DIR` | the secrets directory, needed when a bench names TLS material |
| `--keep-runs N` | keep the newest N ended runs; `run_prune` removes the others |
| `--keep-days D` | keep the runs that ended within D days; `run_prune` removes the older ones |
| `--max-events N` | the events a run's journal keeps before it counts drops |
| `--transport stdio\|http` | `stdio` (the default), or `http`: Streamable HTTP on the loopback, with a token |
| `--http-port PORT` | the port of the HTTP transport, required with `--transport http`; the server does not choose one |
| `--http-token-file FILE` | the file with the bearer token, required with `--transport http` |
| `--http-host ADDRESS` | `127.0.0.1` (the default), `localhost`, or `::1`; no other address is accepted |

The definition tools work without the images. Running a scenario needs both images and a reachable engine.

## HTTP transport

Use the HTTP transport when a client on the same machine cannot start the server itself. It listens on the loopback
only, and every request must carry the token in an `Authorization: Bearer` header.

Create the token, and keep it in a file that only you can read, outside the project:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/igs-mcp-token
chmod 600 ~/igs-mcp-token
```

Start the server, on a port you choose:

```bash
PYTHONPATH=src .venv/bin/python -m igs_iec104_mcp --project ~/igs-project \
  --transport http --http-port 8765 --http-token-file ~/igs-mcp-token
```

The MCP endpoint is `http://127.0.0.1:8765/mcp`. The server logs its address to standard error, never the token.

What the transport checks, and what it does not:

- A request without the token, or with another token, is refused with `401`.
- A request whose `Host` is not the listener's own name is refused with `421`, and one whose `Origin` is not the
  listener's own is refused with `403`. This is the protection against a web page that rebinds a name to the loopback.
- The token file must be owner-only, must not be a symbolic link, must be outside the project, and must hold 32 to 256
  printable characters. An error names the rule, never the token.
- There is no remote listener and no TLS. A host other than the loopback is refused at start. A remote listener needs
  TLS and an authorization server, which is a separate decision.
- The token is the only credential. The server publishes protected-resource metadata that names itself as the issuer,
  since it runs no authorization server: a client is given the token out of band.

## Connect an MCP client

Register the server in the client's configuration. Use absolute paths, and set `PYTHONPATH` to the `src`
directory of the repository. For Podman, also set `DOCKER_HOST` to its socket:

```json
{
  "mcpServers": {
    "igs-iec104": {
      "command": "/home/you/igs-iec104-mcp/.venv/bin/python",
      "args": [
        "-m", "igs_iec104_mcp",
        "--project", "/home/you/igs-project",
        "--runtime-image", "sha256:<runtime identity>",
        "--relay-image", "sha256:<relay identity>"
      ],
      "env": {
        "PYTHONPATH": "/home/you/igs-iec104-mcp/src",
        "DOCKER_HOST": "unix:///run/user/1000/podman/podman.sock"
      }
    }
  }
}
```

Omit `DOCKER_HOST` with Docker Engine. Once the client lists the tools, validate the example bench with
`bench_validate` (its text is `examples/benches/basic-pair.yaml`), then store it with `bench_create`, and run
`examples/scenarios/interrogation-and-spontaneous-update.yaml` with `scenario_run`.

## Podman

Podman provides the Docker API through a socket that a service serves. Start the service for your user:

```bash
mkdir -p /run/user/$(id -u)/podman
podman system service --time=0 unix:///run/user/$(id -u)/podman/podman.sock &
export DOCKER_HOST=unix:///run/user/$(id -u)/podman/podman.sock
```

The service must keep running while a bench runs. Its socket directory must exist before it starts.

## Verify the checkout

These are the gates of the project, the same ones CI runs:

```bash
uv run ruff format --check
uv run ruff check
uv run mypy
uv run pytest tests/unit
uv run python scripts/check_licences.py
```

The Rust gate, from `runtime/`:

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
cargo deny check
```

The integration tests need a running engine and the images. Set the image identities, the local runtime binary,
and the engine socket, then run them:

```bash
export IGS_RUNTIME_IMAGE=sha256:<runtime identity>
export IGS_RELAY_IMAGE=sha256:<relay identity>
export IGS_RUNTIME_BINARY=$PWD/runtime/target/release/igs-iec104-runtime
uv run pytest tests/integration
```

A test that needs a variable that is not set is skipped, except in CI, where the same missing input fails the
run. CI builds the runtime in debug mode for the local-bench tests; the commands above use release, which is the
build the images contain.
