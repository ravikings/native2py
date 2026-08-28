#!/usr/bin/env bash
# Bring up the nativegate console locally.
#
#   ./console/start.sh                # http://localhost:8000
#   ./console/start.sh --port 8010    # http://localhost:8010, if 8000 is taken
#
# Thin wrapper around `docker compose up` (see docker-compose.yml at the repo
# root and console/README.md for what it configures) — kept as a script so
# there's one obvious command to run, with a couple of sanity checks first.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

port="${CONSOLE_PORT:-8000}"
extra_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      if [[ $# -lt 2 ]]; then
        echo "error: --port requires a value" >&2
        exit 1
      fi
      port="$2"
      shift 2
      ;;
    --port=*)
      port="${1#--port=}"
      shift
      ;;
    *)
      extra_args+=("$1")
      shift
      ;;
  esac
done

if ! [[ "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
  echo "error: --port must be a number between 1 and 65535 (got '$port')" >&2
  exit 1
fi
export CONSOLE_PORT="$port"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is not installed or not on PATH" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "error: docker daemon is not reachable (is Docker Desktop / the docker service running?)" >&2
  exit 1
fi

compose=(docker compose)
if ! docker compose version >/dev/null 2>&1; then
  if command -v docker-compose >/dev/null 2>&1; then
    compose=(docker-compose)
  else
    echo "error: neither 'docker compose' nor 'docker-compose' is available" >&2
    exit 1
  fi
fi

echo "Starting the nativegate console (NGATE_AUTH=none, bound to 127.0.0.1:${port})..."
"${compose[@]}" up --build "${extra_args[@]+"${extra_args[@]}"}"
