#!/usr/bin/env bash
# Installs the MCP server on this machine (IMPLEMENTATION.md §14, tasks I4 and I6; issue #1):
# the locked Python environment, the preflight checks, the runtime and relay images built
# and named by their identities, the project directory, a state file that holds those
# identities, and, with --client, the registration with that MCP client (D-061). It never
# installs a system package, uses sudo, or changes a group: a failed check stops the run and
# the preflight names the corrective step (D-056).
#
# Usage: scripts/install.sh [--engine auto|podman|docker] [--project-dir PATH] [--dry-run]
#                          [--client claude|codex|generic [--force] [--yes]]
set -euo pipefail

RUNTIME_TAG=igs-iec104-runtime:local
RELAY_TAG=igs-iec104-relay:local

usage() {
    cat <<'EOF'
usage: scripts/install.sh [--engine auto|podman|docker] [--project-dir PATH] [--dry-run]
                         [--client claude|codex|generic [--force] [--yes]]

  --engine      the container engine: auto (Podman first, then Docker), podman, or docker
  --project-dir the project directory of the definitions (default: ~/igs-project)
  --dry-run     print the steps and change nothing; the preflight runs on a real run only
  --client      register the server with this MCP client, under the name igs-iec104;
                generic prints the entry for any other client, and writes nothing
  --force       replace a registration that differs from the one the installer writes
  --yes         do not ask before a new registration is added
EOF
}

die() {
    echo "install: $1" >&2
    exit "${2:-1}"
}

engine=auto
project_dir="$HOME/igs-project"
dry_run=0
client=
force=0
yes=0
while [ $# -gt 0 ]; do
    case "$1" in
        --engine)
            [ $# -ge 2 ] || die "--engine needs a value" 2
            engine=$2
            shift 2
            ;;
        --project-dir)
            [ $# -ge 2 ] || die "--project-dir needs a path" 2
            project_dir=$2
            shift 2
            ;;
        --dry-run)
            dry_run=1
            shift
            ;;
        --client)
            [ $# -ge 2 ] || die "--client needs claude or codex" 2
            client=$2
            shift 2
            ;;
        --force)
            force=1
            shift
            ;;
        --yes)
            yes=1
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1 (see --help)" 2
            ;;
    esac
done
case "$engine" in
    auto | podman | docker) ;;
    *) die "--engine must be auto, podman, or docker" 2 ;;
esac
case "$client" in
    '' | claude | codex | generic) ;;
    *) die "--client must be claude, codex, or generic" 2 ;;
esac

REPO=$(cd "$(dirname "$0")/.." && pwd -P)
project_dir=$(realpath -m -- "$project_dir")
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/igs-iec104-mcp"
STATE="$STATE_DIR/install.json"

if ! command -v uv >/dev/null 2>&1; then
    die "uv is not on PATH: install it (https://docs.astral.sh/uv/), then run this again" 2
fi

if [ "$dry_run" -eq 1 ]; then
    cat <<EOF
dry run: nothing is changed. A real run would:
  1. uv sync --locked                       (in $REPO)
  2. the preflight, with --engine $engine:  uv run --locked python -m igs_iec104_mcp.doctor
     (a failed check stops the run here)
  3. build the runtime image:  <engine> build --file docker/runtime.Dockerfile --tag $RUNTIME_TAG runtime
  4. build the relay image:    <engine> build --file docker/relay.Dockerfile --tag $RELAY_TAG docker
  5. read each image identity: <engine> image inspect --format '{{.Id}}' TAG
  6. create the project directory $project_dir if it is missing
  7. write the state file $STATE, if its content changes
EOF
    if [ -n "$client" ]; then
        echo "  8. register the server with $client: claude and codex show their commands first, and generic prints the entry and writes nothing"
    fi
    exit 0
fi

cd "$REPO"
# The package is not installed: its source is on the path of every command it runs.
export PYTHONPATH="$REPO/src${PYTHONPATH:+:$PYTHONPATH}"

echo "==> environment"
uv sync --locked

echo "==> preflight"
doctor=(uv run --locked --project "$REPO" python -m igs_iec104_mcp.doctor
    --engine "$engine" --project-dir "$project_dir")
if ! report=$("${doctor[@]}" --json); then
    "${doctor[@]}" || true
    die "the preflight failed: fix the checks above, then run this again" 1
fi
engine_info=$(printf '%s' "$report" | uv run --locked --project "$REPO" python -c '
import json, sys
checks = {check["name"]: check for check in json.load(sys.stdin)["checks"]}
print(checks["engine"]["data"]["engine"] + "\t" + checks["engine"]["data"]["host"])
')
IFS=$'\t' read -r engine_kind docker_host <<<"$engine_info"
case "$engine_kind" in
    podman | docker) ENGINE_CLI=$engine_kind ;;
    *) die "the preflight found an unknown engine: $engine_kind" 1 ;;
esac

echo "==> images, built with $ENGINE_CLI"
"$ENGINE_CLI" build --file docker/runtime.Dockerfile --tag "$RUNTIME_TAG" runtime
"$ENGINE_CLI" build --file docker/relay.Dockerfile --tag "$RELAY_TAG" docker

# An engine reports an image identity as sha256:<hex>, or, for Podman, as <hex> alone.
identity_of() {
    local id
    id=$("$ENGINE_CLI" image inspect --format '{{.Id}}' "$1") ||
        die "cannot read the identity of $1" 1
    id=${id#sha256:}
    [[ $id =~ ^[0-9a-f]{64}$ ]] || die "the identity of $1 is not a sha256 digest: $id" 1
    printf 'sha256:%s\n' "$id"
}
runtime_image=$(identity_of "$RUNTIME_TAG")
relay_image=$(identity_of "$RELAY_TAG")

echo "==> project directory"
mkdir -p -- "$project_dir"

echo "==> state"
state=$(uv run --locked --project "$REPO" python -c '
import json, sys
engine, repository, project, runtime, relay = sys.argv[1:]
print(json.dumps({
    "engine": engine,
    "project_dir": project,
    "relay_image": relay,
    "repository": repository,
    "runtime_image": runtime,
    "schema": 1,
}, indent=2, sort_keys=True))
' "$ENGINE_CLI" "$REPO" "$project_dir" "$runtime_image" "$relay_image")
mkdir -p -- "$STATE_DIR"
if [ -f "$STATE" ] && [ "$(cat -- "$STATE")" = "$state" ]; then
    echo "state unchanged: $STATE"
else
    tmp=$(mktemp "$STATE_DIR/install.json.XXXXXX")
    printf '%s\n' "$state" >"$tmp"
    mv -f -- "$tmp" "$STATE"
    echo "state written: $STATE"
fi

if [ -n "$client" ]; then
    echo "==> registration with $client"
    register=(uv run --locked --project "$REPO" python -m igs_iec104_mcp.registration
        --client "$client" --repository "$REPO" --project-dir "$project_dir"
        --runtime-image "$runtime_image" --relay-image "$relay_image"
        --docker-host "$docker_host")
    [ "$force" -eq 1 ] && register+=(--force)
    [ "$yes" -eq 1 ] && register+=(--yes)
    "${register[@]}" || die "the registration with $client is not done: see above" 1
fi

echo "installed: runtime $runtime_image"
echo "           relay   $relay_image"
echo "           project $project_dir"
if [ "$client" = generic ]; then
    echo "next: copy the entry printed above into your client's configuration, then start a new session"
elif [ -n "$client" ]; then
    echo "next: start a new $client session, and check the server (docs/setup.md#connect-an-mcp-client)"
else
    echo "next: register the server with your MCP client (docs/setup.md#connect-an-mcp-client)"
fi
