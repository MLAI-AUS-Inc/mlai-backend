#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

if [[ $# -eq 0 ]]; then
  printf 'Usage: scripts/local-live-db/django.sh <manage.py command> [args...]\n' >&2
  exit 1
fi

MLAI_ROOT="$(local_live_db_repo_root)"
LOCAL_ENV="$MLAI_ROOT/.env.local-docker"
require_file "$LOCAL_ENV"

COMMAND="$1"

case "$COMMAND" in
  migrate|makemigrations|createsuperuser|flush|sqlflush|loaddata|dbshell)
    printf 'Refusing to run `%s` against the live database profile.\n' "$COMMAND" >&2
    exit 1
    ;;
esac

docker compose -f "$MLAI_ROOT/docker-compose.local.yml" exec web python manage.py "$@"
