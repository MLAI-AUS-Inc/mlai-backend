#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

INSPECTION_FILE="${1:-$(inspection_file)}"

require_file "$INSPECTION_FILE"

MODE="$(json_field "$INSPECTION_FILE" mode)"
REMOTE_HOST="$(json_field "$INSPECTION_FILE" remote_host)"
CONTROL_SOCKET="$(control_socket_path)"
BRIDGE_NAME="$(remote_bridge_name)"

if [[ "$MODE" == "external_live_db" ]]; then
  printf 'Inspection mode is external_live_db; there is no DB tunnel to stop.\n'
  exit 0
fi

if [[ -S "$CONTROL_SOCKET" ]]; then
  ssh -S "$CONTROL_SOCKET" -O exit "$REMOTE_HOST" >/dev/null 2>&1 || true
  rm -f "$CONTROL_SOCKET"
fi

ssh "$REMOTE_HOST" "docker rm -f '$BRIDGE_NAME' >/dev/null 2>&1 || true"

printf 'Stopped the local DB tunnel and removed the remote bridge container.\n'
