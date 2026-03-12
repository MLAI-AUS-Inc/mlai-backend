#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

MLAI_ROOT="$(local_live_db_repo_root)"
CONTENT_FACTORY_ROOT="$(content_factory_repo_root)"
MLAI_ENV="$MLAI_ROOT/.env.local-docker"
CONTENT_FACTORY_ENV="$CONTENT_FACTORY_ROOT/.env.local-docker"
INSPECTION_FILE="$(inspection_file)"
LOCAL_DB_PORT="${LOCAL_DB_TUNNEL_PORT:-15432}"

require_file "$MLAI_ENV"
require_file "$CONTENT_FACTORY_ENV"

MODE=""
if [[ -f "$INSPECTION_FILE" ]]; then
  MODE="$(json_field "$INSPECTION_FILE" mode || true)"
fi

if [[ "$MODE" == "tunneled_live_db" ]] && ! port_is_listening "$LOCAL_DB_PORT"; then
  printf 'Expected the DB tunnel on localhost:%s, but nothing is listening there.\n' "$LOCAL_DB_PORT" >&2
  printf 'Start it first with scripts/local-live-db/start-db-tunnel.sh\n' >&2
  exit 1
fi

docker compose -f "$MLAI_ROOT/docker-compose.local.yml" up --build -d
docker compose -f "$CONTENT_FACTORY_ROOT/docker-compose.local.yml" up --build -d

printf 'MLAI backend is starting at http://localhost:8000\n'
printf 'Content Factory is starting at http://localhost:8001\n'
