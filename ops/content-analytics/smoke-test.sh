#!/bin/sh
set -eu

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

pass() {
  printf 'PASS: %s\n' "$*"
}

for tool in curl grep awk mktemp; do
  command -v "$tool" >/dev/null 2>&1 || fail "$tool is required"
done

: "${PUBLIC_ANALYTICS_ORIGIN:?Set PUBLIC_ANALYTICS_ORIGIN, for example https://analytics.example.com}"
: "${ARTICLE_ORIGIN:?Set ARTICLE_ORIGIN, for example https://customer.example.com}"

PUBLIC_ANALYTICS_ORIGIN=${PUBLIC_ANALYTICS_ORIGIN%/}
TRACKER_SCRIPT_NAME=${TRACKER_SCRIPT_NAME:-content-signal.js}
COLLECT_API_ENDPOINT=${COLLECT_API_ENDPOINT:-/api/content-signal}
TRACKER_SCRIPT_NAME=${TRACKER_SCRIPT_NAME#/}
case "$COLLECT_API_ENDPOINT" in
  /*) ;;
  *) fail "COLLECT_API_ENDPOINT must start with /" ;;
esac

case "$PUBLIC_ANALYTICS_ORIGIN" in
  https://*) curl_transport_args="--proto =https --tlsv1.2" ;;
  http://localhost:*|http://127.0.0.1:*)
    [ "${ALLOW_HTTP_LOCALHOST:-0}" = "1" ] || fail "HTTP is allowed only when ALLOW_HTTP_LOCALHOST=1"
    curl_transport_args=""
    ;;
  *) fail "PUBLIC_ANALYTICS_ORIGIN must use HTTPS" ;;
esac

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT INT TERM

# shellcheck disable=SC2086 # Intentionally expand the curl option list.
curl -fsSL $curl_transport_args \
  -D "$tmpdir/tracker.headers" \
  -o "$tmpdir/tracker.js" \
  "$PUBLIC_ANALYTICS_ORIGIN/$TRACKER_SCRIPT_NAME"

grep -Eqi '^content-type:.*javascript' "$tmpdir/tracker.headers" \
  || fail "tracker response is not JavaScript"
grep -Fq "$COLLECT_API_ENDPOINT" "$tmpdir/tracker.js" \
  || fail "tracker does not embed the configured collection path"
pass "TLS-valid tracker is served with the configured collection path"

# shellcheck disable=SC2086
preflight_status=$(curl -sS $curl_transport_args \
  -o /dev/null \
  -D "$tmpdir/preflight.headers" \
  -w '%{http_code}' \
  -X OPTIONS \
  -H "Origin: $ARTICLE_ORIGIN" \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: content-type' \
  "$PUBLIC_ANALYTICS_ORIGIN$COLLECT_API_ENDPOINT")
case "$preflight_status" in
  2*) ;;
  *) fail "collector CORS preflight returned HTTP $preflight_status" ;;
esac

cors_origin=$(awk '
  tolower($0) ~ /^access-control-allow-origin:/ {
    sub(/^[^:]*:[[:space:]]*/, ""); sub(/\r$/, ""); print; exit
  }
' "$tmpdir/preflight.headers")
case "$cors_origin" in
  '*'|"$ARTICLE_ORIGIN") ;;
  *) fail "collector CORS does not allow $ARTICLE_ORIGIN (got: ${cors_origin:-missing})" ;;
esac
grep -Eqi '^access-control-allow-methods:.*POST' "$tmpdir/preflight.headers" \
  || fail "collector CORS response does not allow POST"
pass "collector CORS preflight allows the article origin"

# This is the important boundary: the public hostname must not expose Umami's
# UI, heartbeat, authentication, or management APIs.
for private_path in / /login /api/heartbeat /api/websites; do
  # shellcheck disable=SC2086
  status=$(curl -sS $curl_transport_args -o /dev/null -w '%{http_code}' \
    "$PUBLIC_ANALYTICS_ORIGIN$private_path")
  case "$status" in
    2*|3*) fail "private Umami route $private_path is publicly reachable (HTTP $status)" ;;
    *) ;;
  esac
done
pass "public ingress denies Umami UI and management API routes"

if [ -n "${ARTICLE_URL:-}" ]; then
  # shellcheck disable=SC2086
  curl -fsSL $curl_transport_args \
    -D "$tmpdir/article.headers" \
    -o "$tmpdir/article.html" \
    "$ARTICLE_URL"
  csp=$(awk '
    tolower($0) ~ /^content-security-policy:/ {
      sub(/^[^:]*:[[:space:]]*/, ""); sub(/\r$/, ""); print; exit
    }
  ' "$tmpdir/article.headers")
  [ -n "$csp" ] || fail "ARTICLE_URL has no Content-Security-Policy response header"
  printf '%s' "$csp" | tr ';' '\n' | grep -E '^[[:space:]]*script-src([[:space:]]|$)' | grep -Fq "$PUBLIC_ANALYTICS_ORIGIN" \
    || fail "CSP script-src does not allow $PUBLIC_ANALYTICS_ORIGIN"
  printf '%s' "$csp" | tr ';' '\n' | grep -E '^[[:space:]]*connect-src([[:space:]]|$)' | grep -Fq "$PUBLIC_ANALYTICS_ORIGIN" \
    || fail "CSP connect-src does not allow $PUBLIC_ANALYTICS_ORIGIN"
  pass "article CSP allows the required analytics script/connect origin"
else
  printf 'SKIP: set ARTICLE_URL to enforce the production CSP gate\n'
fi

printf 'Analytics ingress smoke tests completed successfully.\n'
