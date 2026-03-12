#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

INSPECTION_FILE="${1:-$(inspection_file)}"
LOCAL_DB_PORT="${LOCAL_DB_TUNNEL_PORT:-15432}"

require_file "$INSPECTION_FILE"

MODE="$(json_field "$INSPECTION_FILE" mode)"
REMOTE_HOST="$(json_field "$INSPECTION_FILE" remote_host)"
REMOTE_APP_DIR="$(json_field "$INSPECTION_FILE" remote_app_dir)"
REMOTE_COMPOSE_FILE="$(json_field "$INSPECTION_FILE" remote_compose_file)"
CONTROL_SOCKET="$(control_socket_path)"
BRIDGE_NAME="$(remote_bridge_name)"

if [[ "$MODE" == "external_live_db" ]]; then
  printf 'Inspection mode is external_live_db; no SSH tunnel is required.\n'
  exit 0
fi

if [[ "$MODE" != "tunneled_live_db" ]]; then
  printf 'Inspection mode must be tunneled_live_db, got: %s\n' "$MODE" >&2
  exit 1
fi

if [[ -S "$CONTROL_SOCKET" ]] && ssh -S "$CONTROL_SOCKET" -O check "$REMOTE_HOST" >/dev/null 2>&1; then
  printf 'Local DB tunnel is already running.\n'
  exit 0
fi

if port_is_listening "$LOCAL_DB_PORT"; then
  printf 'Port %s is already in use on localhost.\n' "$LOCAL_DB_PORT" >&2
  exit 1
fi

ssh "$REMOTE_HOST" "bash -s" -- "$REMOTE_APP_DIR" "$REMOTE_COMPOSE_FILE" "$LOCAL_DB_PORT" "$BRIDGE_NAME" <<'REMOTE'
set -euo pipefail

APP_DIR="$1"
COMPOSE_FILE="$2"
LOCAL_PORT="$3"
BRIDGE_NAME="$4"

cd "$APP_DIR"

if docker compose version >/dev/null 2>&1; then
  compose=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose=(docker-compose)
else
  printf 'docker compose is not available on the remote host.\n' >&2
  exit 1
fi

db_container_id="$("${compose[@]}" -f "$COMPOSE_FILE" ps -q db)"
if [[ -z "$db_container_id" ]]; then
  printf 'Could not resolve the remote db container.\n' >&2
  exit 1
fi

db_network="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{println $k}}{{end}}' "$db_container_id" | head -n 1 | tr -d '\r')"
if [[ -z "$db_network" ]]; then
  printf 'Could not resolve the remote db network.\n' >&2
  exit 1
fi

docker rm -f "$BRIDGE_NAME" >/dev/null 2>&1 || true
docker run -d --rm \
  --name "$BRIDGE_NAME" \
  --network "$db_network" \
  -p "127.0.0.1:${LOCAL_PORT}:${LOCAL_PORT}" \
  alpine/socat \
  -d -d "TCP-LISTEN:${LOCAL_PORT},fork,reuseaddr" "TCP:db:5432" >/dev/null
REMOTE

rm -f "$CONTROL_SOCKET"
ssh -M -S "$CONTROL_SOCKET" -fnNT -L "${LOCAL_DB_PORT}:127.0.0.1:${LOCAL_DB_PORT}" "$REMOTE_HOST"

if ! port_is_listening "$LOCAL_DB_PORT"; then
  printf 'Tunnel did not come up on localhost:%s\n' "$LOCAL_DB_PORT" >&2
  exit 1
fi

printf 'Local DB tunnel is up at postgres://host.docker.internal:%s via %s\n' "$LOCAL_DB_PORT" "$REMOTE_HOST"
