#!/bin/sh
# Checks that the public listmonk hostname serves only subscriber routes, and,
# when run on the host, that listmonk itself is healthy on loopback.
set -eu

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

pass() {
  printf 'PASS: %s\n' "$*"
}

for tool in curl grep mktemp; do
  command -v "$tool" >/dev/null 2>&1 || fail "$tool is required"
done

: "${PUBLIC_LISTMONK_ORIGIN:?Set PUBLIC_LISTMONK_ORIGIN, for example https://lists.mlai.au}"
PUBLIC_LISTMONK_ORIGIN=${PUBLIC_LISTMONK_ORIGIN%/}
LOCAL_LISTMONK_URL=${LOCAL_LISTMONK_URL:-}

# ORIGIN_IP skips DNS and Cloudflare and checks nginx on that address directly,
# trusting only ORIGIN_CA (the self-signed origin certificate).
ORIGIN_IP=${ORIGIN_IP:-}
ORIGIN_CA=${ORIGIN_CA:-/etc/nginx/ssl/listmonk-selfsigned.crt}

case "$PUBLIC_LISTMONK_ORIGIN" in
  https://*)
    curl_transport_args="--proto =https --tlsv1.2"
    if [ -n "$ORIGIN_IP" ]; then
      host=${PUBLIC_LISTMONK_ORIGIN#https://}
      [ -r "$ORIGIN_CA" ] || fail "ORIGIN_CA $ORIGIN_CA is not readable"
      curl_transport_args="$curl_transport_args --resolve $host:443:$ORIGIN_IP --cacert $ORIGIN_CA"
    fi
    ;;
  http://localhost:*|http://127.0.0.1:*)
    [ "${ALLOW_HTTP_LOCALHOST:-0}" = "1" ] || fail "HTTP is allowed only when ALLOW_HTTP_LOCALHOST=1"
    curl_transport_args=""
    ;;
  *) fail "PUBLIC_LISTMONK_ORIGIN must use HTTPS" ;;
esac

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT INT TERM
marker="not available"
zero=00000000-0000-0000-0000-000000000000

# request METHOD PATH -> prints the status code; the body goes to $tmpdir/body.
request() {
  # shellcheck disable=SC2086 # Intentionally expand the curl option list.
  curl -sS $curl_transport_args -X "$1" -o "$tmpdir/body" -w '%{http_code}' \
    "$PUBLIC_LISTMONK_ORIGIN$2"
}

expect_closed() {
  status=$(request "$1" "$2")
  if [ "$status" != "404" ] || ! grep -q "$marker" "$tmpdir/body"; then
    fail "$1 $2 should be closed by the ingress (got $status)"
  fi
  pass "$1 $2 is closed"
}

expect_proxied() {
  status=$(request "$1" "$2")
  case "$status" in
    000|403|5??) fail "$1 $2 did not reach listmonk (got $status)" ;;
  esac
  if grep -q "$marker" "$tmpdir/body"; then
    fail "$1 $2 was closed by the ingress but should reach listmonk"
  fi
  pass "$1 $2 reaches listmonk ($status)"
}

if [ -n "$LOCAL_LISTMONK_URL" ]; then
  curl -fsS -o /dev/null "${LOCAL_LISTMONK_URL%/}/health" || fail "listmonk is not healthy at $LOCAL_LISTMONK_URL"
  pass "listmonk is healthy on loopback"
fi

status=$(request GET /public/static/style.css)
[ "$status" = "200" ] || fail "GET /public/static/style.css returned $status"
pass "static assets are served"

expect_proxied GET "/subscription/$zero/$zero"
expect_proxied GET "/link/$zero/$zero/$zero"
expect_proxied GET "/campaign/$zero/$zero"
expect_proxied GET /robots.txt

expect_closed GET /
expect_closed GET /admin
expect_closed GET /admin/login
expect_closed GET /api/health
expect_closed GET /api/subscribers
expect_closed POST /api/public/subscription
expect_closed GET /subscription/form
expect_closed GET /archive
expect_closed POST /webhooks/bounce

printf 'All listmonk ingress checks passed for %s\n' "$PUBLIC_LISTMONK_ORIGIN"
