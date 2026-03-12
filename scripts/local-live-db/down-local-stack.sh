#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

MLAI_ROOT="$(local_live_db_repo_root)"
CONTENT_FACTORY_ROOT="$(content_factory_repo_root)"

docker compose -f "$CONTENT_FACTORY_ROOT/docker-compose.local.yml" down
docker compose -f "$MLAI_ROOT/docker-compose.local.yml" down

printf 'Stopped the local mlai-backend and content-factory containers.\n'
