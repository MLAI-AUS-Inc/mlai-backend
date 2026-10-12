#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$SCRIPT_DIR"
load_config() {
  python3 config.py validate --output .env
  while IFS='=' read -r name value; do
    [[ -z "$name" || "$name" == \#* ]] && continue
    export "$name=$value"
  done < .env
}
compose() { docker compose --project-name mlai-chat-staging-api --env-file .env -f compose.yml "$@" </dev/null; }
case "${1:-help}" in
  init)
    python3 config.py init --output .env --source-sha "$(git -C "$REPO_DIR" rev-parse HEAD)" "${@:2}" ;;
  check)
    load_config
    compose config --quiet ;;
  build|start|migrate)
    load_config
    metadata=$(mktemp)
    trap 'rm -f "$metadata"' EXIT
    if [[ "$1" == build ]]; then
      [[ -z "$(git -C "$REPO_DIR" status --porcelain)" ]] || { echo 'Commit source changes before building an identified release.' >&2; exit 1; }
      export STAGING_API_SOURCE_SHA="$(git -C "$REPO_DIR" rev-parse HEAD)"
      compose build staging-api
      docker image inspect "mlai-chat-staging-api:$STAGING_API_SOURCE_SHA" > "$metadata"
      python3 config.py record-build --output .env --metadata "$metadata" --source-sha "$STAGING_API_SOURCE_SHA"
    else
      [[ -n "$STAGING_API_IMAGE_ID" ]] || { echo 'Build and record the API image first.' >&2; exit 1; }
      docker image inspect "mlai-chat-staging-api:$STAGING_API_SOURCE_SHA" > "$metadata"
      python3 config.py check-image --output .env --metadata "$metadata"
      compose up -d --no-build --wait postgres-api redis-api
      if [[ "$1" == migrate ]]; then
        compose run --rm --no-deps staging-api python manage.py migrate --noinput
      else
        compose up -d --no-build --pull never --wait staging-api
      fi
    fi ;;
  status|stop)
    load_config
    if [[ "$1" == status ]]; then compose ps; else compose stop; fi ;;
  *) echo 'Usage: ./run.sh init --chat-env /path/to/staging-chat/.env | check | build | migrate | start | status | stop' ;;
esac
