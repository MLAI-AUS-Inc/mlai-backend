#!/usr/bin/env bash
set -euo pipefail

URL="${URL:-http://127.0.0.1/healthz/ready}"
SERVICE_NAME="${SERVICE_NAME:-web}"
REQUESTS="${REQUESTS:-100}"
CONCURRENCY="${CONCURRENCY:-20}"
CURL_MAX_TIME_SECONDS="${CURL_MAX_TIME_SECONDS:-5}"
CLOSE_WAIT_MAX_DELTA="${CLOSE_WAIT_MAX_DELTA:-2}"
P95_DEGRADATION_MAX_PERCENT="${P95_DEGRADATION_MAX_PERCENT:-50}"
COMPOSE_CMD="${DOCKER_COMPOSE_CMD:-docker compose}"
TMPDIR="$(mktemp -d)"
RESULTS_FILE="${TMPDIR}/curl-results.tsv"

cleanup() {
  rm -rf "${TMPDIR}"
}
trap cleanup EXIT

container_id="$(${COMPOSE_CMD} ps -q "${SERVICE_NAME}" 2>/dev/null | head -n 1 || true)"

tcp_state_counts() {
  if [[ -n "${container_id}" ]]; then
    docker exec "${container_id}" sh -lc "awk 'NR>1 {s[\$4]++} END {for (k in s) print k,s[k]}' /proc/net/tcp" 2>/dev/null || true
  else
    awk 'NR>1 {s[$4]++} END {for (k in s) print k,s[k]}' /proc/net/tcp 2>/dev/null || true
  fi
}

close_wait_count() {
  tcp_state_counts | awk '$1 == "08" {print $2 + 0; found=1} END {if (!found) print 0}'
}

fd_count() {
  if [[ -z "${container_id}" ]]; then
    echo "container_missing"
    return
  fi
  docker exec "${container_id}" sh -lc '
    for p in /proc/[0-9]*; do
      pid=${p#/proc/}
      cmd=$(tr "\0" " " < "$p/cmdline" 2>/dev/null || true)
      case "$cmd" in
        *gunicorn*mlai.wsgi*) printf "%s " "$pid"; ls "$p/fd" 2>/dev/null | wc -l ;;
      esac
    done
  ' 2>/dev/null || true
}

worker_states() {
  if [[ -z "${container_id}" ]]; then
    echo "container_missing"
    return
  fi
  docker exec "${container_id}" sh -lc '
    for p in /proc/[0-9]*; do
      pid=${p#/proc/}
      cmd=$(tr "\0" " " < "$p/cmdline" 2>/dev/null || true)
      case "$cmd" in
        *gunicorn*mlai.wsgi*)
          state=$(awk "{print \$3}" "$p/stat" 2>/dev/null || true)
          printf "pid=%s state=%s cmd=%s\n" "$pid" "$state" "$cmd"
          ;;
      esac
    done
  ' 2>/dev/null || true
}

socket_fd_details() {
  if [[ -z "${container_id}" ]]; then
    echo "container_missing"
    return
  fi
  docker exec "${container_id}" sh -lc '
    for p in /proc/[0-9]*; do
      pid=${p#/proc/}
      cmd=$(tr "\0" " " < "$p/cmdline" 2>/dev/null || true)
      case "$cmd" in
        *gunicorn*mlai.wsgi*)
          echo "PID=${pid}"
          if command -v lsof >/dev/null 2>&1; then
            lsof -nP -p "$pid" 2>/dev/null | grep TCP || true
          else
            find "$p/fd" -maxdepth 1 -type l -lname "socket:*" 2>/dev/null | wc -l
          fi
          ;;
      esac
    done
  ' 2>/dev/null || true
}

listener_backlog() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | awk 'NR == 1 || /:80 |:8000 /'
  else
    echo "ss_unavailable"
  fi
}

run_batch() {
  local batch_name="$1"
  local total="$2"
  seq 1 "${total}" | xargs -n 1 -P "${CONCURRENCY}" sh -c '
    started=$(date +%s%3N)
    output=$(curl -sS -o /dev/null -H "Connection: close" -H "X-Forwarded-Proto: https" -w "%{http_code}\t%{time_total}\t%{size_download}" --max-time "$1" "$2" 2>&1)
    rc=$?
    ended=$(date +%s%3N)
    printf "%s\t%s\t%s\t%s\t%s\n" "$3" "$rc" "$ended" "$started" "$output"
  ' sh "${CURL_MAX_TIME_SECONDS}" "${URL}" "${batch_name}" >> "${RESULTS_FILE}"
}

summarize_batch() {
  local batch_name="$1"
  local counts
  local p95
  counts="$(awk -F '\t' -v batch="${batch_name}" '
    $1 == batch {
      total++
      if ($2 != 0 || $5 !~ /^2/) failures++
    }
    END { printf "total=%d failures=%d", total + 0, failures + 0 }
  ' "${RESULTS_FILE}")"
  p95="$(awk -F '\t' -v batch="${batch_name}" '$1 == batch {print $6}' "${RESULTS_FILE}" | sort -n | awk '
    { values[++n] = $1 }
    END {
      if (!n) {
        print "0"
        exit
      }
      idx = int(n * 0.95)
      if (idx < 1) idx = 1
      print values[idx]
    }
  ')"
  echo "batch=${batch_name} ${counts} p95=${p95}"
}

echo "[socket-smoke] url=${URL} service=${SERVICE_NAME} requests=${REQUESTS} concurrency=${CONCURRENCY}"
echo "[socket-smoke] listener_backlog_before"
listener_backlog
echo "[socket-smoke] tcp_states_before"
tcp_state_counts | sort
echo "[socket-smoke] fd_counts_before"
fd_count
echo "[socket-smoke] worker_states_before"
worker_states
echo "[socket-smoke] socket_fd_details_before"
socket_fd_details

close_wait_before="$(close_wait_count)"
half_requests=$(( REQUESTS / 2 ))
remaining_requests=$(( REQUESTS - half_requests ))

run_batch first "${half_requests}"
sleep 2
run_batch second "${remaining_requests}"
sleep 2

close_wait_after="$(close_wait_count)"

echo "[socket-smoke] listener_backlog_after"
listener_backlog
echo "[socket-smoke] tcp_states_after"
tcp_state_counts | sort
echo "[socket-smoke] fd_counts_after"
fd_count
echo "[socket-smoke] worker_states_after"
worker_states
echo "[socket-smoke] socket_fd_details_after"
socket_fd_details
echo "[socket-smoke] results"
summarize_batch first
summarize_batch second

summary_counts="$(awk -F '\t' '
  {
    total++
    if ($2 != 0 || $5 !~ /^2/) failures++
    if ($7 == 0) partials++
  }
  END { printf "total=%d failures=%d partials=%d", total + 0, failures + 0, partials + 0 }
' "${RESULTS_FILE}")"
summary_p95="$(awk -F '\t' '{print $6}' "${RESULTS_FILE}" | sort -n | awk '
  { values[++n] = $1 }
  END {
    if (!n) {
      print "0"
      exit
    }
    idx = int(n * 0.95)
    if (idx < 1) idx = 1
    print values[idx]
  }
')"
summary="${summary_counts} p95=${summary_p95}"
echo "[socket-smoke] summary ${summary}"
echo "[socket-smoke] close_wait_before=${close_wait_before} close_wait_after=${close_wait_after} max_delta=${CLOSE_WAIT_MAX_DELTA}"

close_wait_delta=$(( close_wait_after - close_wait_before ))
if (( close_wait_delta > CLOSE_WAIT_MAX_DELTA )); then
  echo "[socket-smoke] FAIL close_wait_growth delta=${close_wait_delta}"
  exit 1
fi

first_p95="$(summarize_batch first | awk -F 'p95=' '{print $2}')"
second_p95="$(summarize_batch second | awk -F 'p95=' '{print $2}')"
degradation_percent="$(awk -v first="${first_p95}" -v second="${second_p95}" 'BEGIN { if (first <= 0) print 0; else printf "%.0f", ((second - first) / first) * 100 }')"
if (( degradation_percent > P95_DEGRADATION_MAX_PERCENT )); then
  echo "[socket-smoke] FAIL p95_degradation_percent=${degradation_percent}"
  exit 1
fi

failures="$(awk -F '\t' '{if ($2 != 0 || $5 !~ /^2/) failures++} END {print failures + 0}' "${RESULTS_FILE}")"
if (( failures > 0 )); then
  echo "[socket-smoke] FAIL request_failures=${failures}"
  exit 1
fi

echo "[socket-smoke] PASS close_wait_stable=true p95_degradation_percent=${degradation_percent}"
