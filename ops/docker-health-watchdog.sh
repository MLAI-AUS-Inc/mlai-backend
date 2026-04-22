#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${WATCHDOG_SERVICE_NAME:?WATCHDOG_SERVICE_NAME is required}"
CHECK_INTERVAL_SECONDS="${WATCHDOG_INTERVAL_SECONDS:-60}"
MAX_RESTARTS="${WATCHDOG_MAX_RESTARTS:-3}"
RESTART_WINDOW_SECONDS="${WATCHDOG_RESTART_WINDOW_SECONDS:-600}"
COMPOSE_CMD="${DOCKER_COMPOSE_CMD:-docker compose}"
restart_times=()

prune_restart_times() {
  local now="$1"
  local kept=()
  local ts
  for ts in "${restart_times[@]}"; do
    if (( now - ts < RESTART_WINDOW_SECONDS )); then
      kept+=("${ts}")
    fi
  done
  restart_times=("${kept[@]}")
}

while true; do
  container_id="$(${COMPOSE_CMD} ps -q "${SERVICE_NAME}" 2>/dev/null | head -n 1 || true)"

  if [[ -z "${container_id}" ]]; then
    echo "[watchdog] service=${SERVICE_NAME} container=missing action=up"
    ${COMPOSE_CMD} up -d "${SERVICE_NAME}" || true
    sleep "${CHECK_INTERVAL_SECONDS}"
    continue
  fi

  health_status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${container_id}" 2>/dev/null || echo unknown)"
  echo "[watchdog] service=${SERVICE_NAME} container=${container_id} health=${health_status}"

  if [[ "${health_status}" == "unhealthy" ]]; then
    now="$(date +%s)"
    prune_restart_times "${now}"
    if (( ${#restart_times[@]} >= MAX_RESTARTS )); then
      echo "[watchdog] service=${SERVICE_NAME} action=alert rate_limited=true restart_count=${#restart_times[@]} restart_window_seconds=${RESTART_WINDOW_SECONDS}"
      sleep "${CHECK_INTERVAL_SECONDS}"
      continue
    fi

    echo "[watchdog] service=${SERVICE_NAME} action=restart"
    ${COMPOSE_CMD} restart "${SERVICE_NAME}" || true
    restart_times+=("${now}")
  fi

  sleep "${CHECK_INTERVAL_SECONDS}"
done
