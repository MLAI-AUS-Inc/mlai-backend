#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

matches="$(
  rg -n '^\s*(import requests|from requests)' \
    --glob '*.py' \
    --glob '!integrations/http_client.py' \
    --glob '!**/tests*.py' \
    --glob '!**/test_*.py' \
    --glob '!**/verify_*.py' \
    --glob '!**/migrations/**' \
    . || true
)"

if [[ -n "${matches}" ]]; then
  echo "Raw requests imports are not allowed outside integrations/http_client.py."
  echo "Use: from integrations import http_client as http_requests"
  echo "${matches}"
  exit 1
fi

echo "HTTP wrapper check passed."
